from collections.abc import Sequence
from datetime import timedelta
from typing import LiteralString
from uuid import UUID

from psycopg.rows import TupleRow

from stockroom.fulfillment.domain import Shipment, ShipmentLine, ShipmentStatus
from stockroom.shared.db import Connection

_SELECT: LiteralString = (
    "SELECT id, order_id, reservation_id, status, attempts, next_attempt_at, last_error, "
    "tracking_number FROM fulfillment.shipments "
)


async def try_insert_shipment(
    conn: Connection,
    shipment_id: UUID,
    order_id: UUID,
    reservation_id: UUID,
    lines: Sequence[ShipmentLine],
) -> bool:
    cursor = await conn.execute(
        """
        INSERT INTO fulfillment.shipments (id, order_id, reservation_id, status)
        VALUES (%s, %s, %s, 'picked')
        ON CONFLICT (order_id) DO NOTHING
        RETURNING id
        """,
        (shipment_id, order_id, reservation_id),
    )
    if await cursor.fetchone() is None:
        return False
    await conn.execute(
        """
        INSERT INTO fulfillment.shipment_lines (shipment_id, sku, warehouse_id, quantity)
        SELECT %s, sku, warehouse_id, quantity
        FROM unnest(%s::text[], %s::text[], %s::integer[]) AS l (sku, warehouse_id, quantity)
        """,
        (
            shipment_id,
            [line.sku for line in lines],
            [line.warehouse_id for line in lines],
            [line.quantity for line in lines],
        ),
    )
    return True


async def insert_tombstone(conn: Connection, shipment_id: UUID, order_id: UUID) -> None:
    await conn.execute(
        """
        INSERT INTO fulfillment.shipments (id, order_id, status, last_error)
        VALUES (%s, %s, 'cancelled', 'order cancelled before fulfillment started')
        ON CONFLICT (order_id) DO NOTHING
        """,
        (shipment_id, order_id),
    )


async def lock_next_due(conn: Connection) -> Shipment | None:
    """Claim the oldest shipment waiting for the carrier.

    SKIP LOCKED lets several dispatch workers run side by side, each on its own row.
    The lock is held for the carrier call, so a cancellation for this order waits for
    the call's outcome instead of racing it.
    """
    cursor = await conn.execute(
        _SELECT + "WHERE status = 'picked' AND next_attempt_at <= now() "
        "ORDER BY next_attempt_at LIMIT 1 FOR UPDATE SKIP LOCKED"
    )
    row = await cursor.fetchone()
    return None if row is None else await _hydrate(conn, row)


async def get_by_order(conn: Connection, order_id: UUID, *, lock: bool = False) -> Shipment | None:
    cursor = await conn.execute(
        _SELECT + "WHERE order_id = %s" + (" FOR UPDATE" if lock else ""),
        (order_id,),
    )
    row = await cursor.fetchone()
    return None if row is None else await _hydrate(conn, row)


async def list_shipments(
    conn: Connection, status: ShipmentStatus | None, limit: int
) -> list[Shipment]:
    cursor = await conn.execute(
        _SELECT + "WHERE %(status)s::text IS NULL OR status = %(status)s "
        "ORDER BY created_at DESC LIMIT %(limit)s",
        {"status": status.value if status else None, "limit": limit},
    )
    return [await _hydrate(conn, row) for row in await cursor.fetchall()]


async def mark_shipped(conn: Connection, shipment_id: UUID, tracking_number: str) -> None:
    await conn.execute(
        """
        UPDATE fulfillment.shipments
        SET status = 'shipped', attempts = attempts + 1, tracking_number = %s,
            last_error = NULL, updated_at = now()
        WHERE id = %s
        """,
        (tracking_number, shipment_id),
    )


async def schedule_retry(conn: Connection, shipment_id: UUID, error: str, delay: timedelta) -> None:
    await conn.execute(
        """
        UPDATE fulfillment.shipments
        SET attempts = attempts + 1, last_error = %s, next_attempt_at = now() + %s,
            updated_at = now()
        WHERE id = %s
        """,
        (error, delay, shipment_id),
    )


async def mark_final(
    conn: Connection, shipment_id: UUID, status: ShipmentStatus, error: str | None
) -> None:
    await conn.execute(
        """
        UPDATE fulfillment.shipments
        SET status = %s, attempts = attempts + %s, last_error = coalesce(%s, last_error),
            updated_at = now()
        WHERE id = %s
        """,
        (status.value, 1 if status is ShipmentStatus.FAILED else 0, error, shipment_id),
    )


async def _hydrate(conn: Connection, row: TupleRow) -> Shipment:
    shipment_id, order_id, reservation_id, status, attempts, next_at, error, tracking = row
    cursor = await conn.execute(
        """
        SELECT sku, warehouse_id, quantity FROM fulfillment.shipment_lines
        WHERE shipment_id = %s ORDER BY sku, warehouse_id
        """,
        (shipment_id,),
    )
    lines = tuple(ShipmentLine(*line) for line in await cursor.fetchall())
    return Shipment(
        id=shipment_id,
        order_id=order_id,
        reservation_id=reservation_id,
        status=ShipmentStatus(status),
        attempts=attempts,
        next_attempt_at=next_at,
        last_error=error,
        tracking_number=tracking,
        lines=lines,
    )
