from collections.abc import Sequence
from typing import LiteralString
from uuid import UUID

from stockroom.inventory.domain import (
    Reservation,
    ReservationStatus,
    StockKey,
    StockLevel,
    resolve_status,
)
from stockroom.shared.db import Connection


def _columns(keys: Sequence[StockKey]) -> tuple[list[str], list[str]]:
    return [key.sku for key in keys], [key.warehouse_id for key in keys]


async def try_insert_reservation(
    conn: Connection, reservation_id: UUID, order_id: str, ttl_seconds: int
) -> bool:
    """False when the order already has a reservation.

    A concurrent insert of the same order blocks here on the unique index until the other
    transaction ends, so retries of one order never race each other past this point.
    """
    cursor = await conn.execute(
        """
        INSERT INTO inventory.reservations (id, order_id, status, expires_at)
        VALUES (%s, %s, 'active', now() + make_interval(secs => %s))
        ON CONFLICT (order_id) DO NOTHING
        RETURNING id
        """,
        (reservation_id, order_id, ttl_seconds),
    )
    return await cursor.fetchone() is not None


async def lock_stock_items(conn: Connection, keys: Sequence[StockKey]) -> set[StockKey]:
    """Lock the rows in key order and return the ones that exist."""
    cursor = await conn.execute(
        """
        SELECT si.sku, si.warehouse_id
        FROM inventory.stock_items si
        JOIN unnest(%s::text[], %s::text[]) AS k (sku, warehouse_id)
          ON k.sku = si.sku AND k.warehouse_id = si.warehouse_id
        ORDER BY si.sku, si.warehouse_id
        FOR UPDATE OF si
        """,
        _columns(keys),
    )
    return {StockKey(sku, warehouse_id) for sku, warehouse_id in await cursor.fetchall()}


async def available_quantities(conn: Connection, keys: Sequence[StockKey]) -> dict[StockKey, int]:
    cursor = await conn.execute(
        """
        SELECT sl.sku, sl.warehouse_id, sl.available
        FROM inventory.stock_levels sl
        JOIN unnest(%s::text[], %s::text[]) AS k (sku, warehouse_id)
          ON k.sku = sl.sku AND k.warehouse_id = sl.warehouse_id
        """,
        _columns(keys),
    )
    return {StockKey(sku, wh): available for sku, wh, available in await cursor.fetchall()}


async def insert_reservation_lines(
    conn: Connection, reservation_id: UUID, lines: dict[StockKey, int]
) -> None:
    skus, warehouses = _columns(list(lines))
    await conn.execute(
        """
        INSERT INTO inventory.reservation_lines (reservation_id, sku, warehouse_id, quantity)
        SELECT %s, sku, warehouse_id, quantity
        FROM unnest(%s::text[], %s::text[], %s::integer[]) AS l (sku, warehouse_id, quantity)
        """,
        (reservation_id, skus, warehouses, list(lines.values())),
    )


async def _load_reservation(
    conn: Connection, where: LiteralString, value: object, *, lock: bool
) -> Reservation | None:
    query: LiteralString = (
        "SELECT id, order_id, status, expires_at, statement_timestamp() "
        "FROM inventory.reservations WHERE " + where + (" FOR UPDATE" if lock else "")
    )
    cursor = await conn.execute(query, (value,))
    row = await cursor.fetchone()
    if row is None:
        return None
    reservation_id, order_id, status, expires_at, now = row
    cursor = await conn.execute(
        """
        SELECT sku, warehouse_id, quantity FROM inventory.reservation_lines
        WHERE reservation_id = %s ORDER BY sku, warehouse_id
        """,
        (reservation_id,),
    )
    lines = {StockKey(sku, wh): quantity for sku, wh, quantity in await cursor.fetchall()}
    return Reservation(
        id=reservation_id,
        order_id=order_id,
        status=resolve_status(ReservationStatus(status), expires_at, now),
        expires_at=expires_at,
        lines=lines,
    )


async def get_reservation(
    conn: Connection, reservation_id: UUID, *, lock: bool = False
) -> Reservation | None:
    return await _load_reservation(conn, "id = %s", reservation_id, lock=lock)


async def get_reservation_by_order(conn: Connection, order_id: str) -> Reservation | None:
    return await _load_reservation(conn, "order_id = %s", order_id, lock=False)


async def set_status(conn: Connection, reservation_id: UUID, status: ReservationStatus) -> None:
    await conn.execute(
        "UPDATE inventory.reservations SET status = %s, updated_at = now() WHERE id = %s",
        (status.value, reservation_id),
    )


async def record_shipment(conn: Connection, reservation_id: UUID) -> None:
    await conn.execute(
        """
        INSERT INTO inventory.stock_movements (sku, warehouse_id, quantity, reason, reference)
        SELECT sku, warehouse_id, -quantity, 'shipment', %s
        FROM inventory.reservation_lines WHERE reservation_id = %s
        """,
        (str(reservation_id), reservation_id),
    )


async def record_receipt(
    conn: Connection, key: StockKey, quantity: int, reference: str | None
) -> None:
    await conn.execute(
        """
        INSERT INTO inventory.stock_items (sku, warehouse_id) VALUES (%s, %s)
        ON CONFLICT DO NOTHING
        """,
        (key.sku, key.warehouse_id),
    )
    await conn.execute(
        """
        INSERT INTO inventory.stock_movements (sku, warehouse_id, quantity, reason, reference)
        VALUES (%s, %s, %s, 'receipt', %s)
        """,
        (key.sku, key.warehouse_id, quantity, reference),
    )


async def list_stock_levels(conn: Connection, warehouse_id: str | None) -> list[StockLevel]:
    cursor = await conn.execute(
        """
        SELECT sku, warehouse_id, on_hand, reserved, available
        FROM inventory.stock_levels
        WHERE %(warehouse_id)s::text IS NULL OR warehouse_id = %(warehouse_id)s
        ORDER BY warehouse_id, sku
        """,
        {"warehouse_id": warehouse_id},
    )
    return [
        StockLevel(StockKey(sku, wh), on_hand, reserved, available)
        for sku, wh, on_hand, reserved, available in await cursor.fetchall()
    ]
