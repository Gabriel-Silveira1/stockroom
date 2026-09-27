from collections.abc import Sequence
from uuid import UUID

from stockroom.orders.domain import HistoryEntry, Order, OrderLine, OrderStatus
from stockroom.shared.db import Connection


async def claim_idempotency_key(
    conn: Connection, key: str, request_hash: str, order_id: UUID
) -> bool:
    """False when the key was already used.

    A concurrent request with the same key blocks here until the first one commits or
    rolls back, so only one of them ever creates an order.
    """
    cursor = await conn.execute(
        """
        INSERT INTO orders.idempotency_keys (key, request_hash, order_id, status_code)
        VALUES (%s, %s, %s, 201)
        ON CONFLICT (key) DO NOTHING
        RETURNING key
        """,
        (key, request_hash, order_id),
    )
    return await cursor.fetchone() is not None


async def get_idempotency_record(conn: Connection, key: str) -> tuple[str, UUID, int]:
    cursor = await conn.execute(
        "SELECT request_hash, order_id, status_code FROM orders.idempotency_keys WHERE key = %s",
        (key,),
    )
    row = await cursor.fetchone()
    assert row is not None
    request_hash, order_id, status_code = row
    return request_hash, order_id, status_code


async def point_idempotency_key(
    conn: Connection, key: str, order_id: UUID, status_code: int
) -> None:
    await conn.execute(
        "UPDATE orders.idempotency_keys SET order_id = %s, status_code = %s WHERE key = %s",
        (order_id, status_code, key),
    )


async def try_insert_order(
    conn: Connection, order_id: UUID, external_id: str, lines: Sequence[OrderLine]
) -> bool:
    """False when an order with this external id already exists."""
    cursor = await conn.execute(
        """
        INSERT INTO orders.orders (id, external_id, status) VALUES (%s, %s, 'pending')
        ON CONFLICT (external_id) DO NOTHING
        RETURNING id
        """,
        (order_id, external_id),
    )
    if await cursor.fetchone() is None:
        return False
    await conn.execute(
        """
        INSERT INTO orders.order_lines (order_id, sku, warehouse_id, quantity)
        SELECT %s, sku, warehouse_id, quantity
        FROM unnest(%s::text[], %s::text[], %s::integer[]) AS l (sku, warehouse_id, quantity)
        """,
        (
            order_id,
            [line.sku for line in lines],
            [line.warehouse_id for line in lines],
            [line.quantity for line in lines],
        ),
    )
    return True


async def get_order_id_by_external(conn: Connection, external_id: str) -> UUID:
    cursor = await conn.execute(
        "SELECT id FROM orders.orders WHERE external_id = %s", (external_id,)
    )
    row = await cursor.fetchone()
    assert row is not None
    order_id: UUID = row[0]
    return order_id


async def get_order(conn: Connection, order_id: UUID, *, lock: bool = False) -> Order | None:
    cursor = await conn.execute(
        """
        SELECT id, external_id, status, cancel_reason, reservation_id, created_at
        FROM orders.orders WHERE id = %s
        """
        + (" FOR UPDATE" if lock else ""),
        (order_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    cursor = await conn.execute(
        """
        SELECT sku, warehouse_id, quantity FROM orders.order_lines
        WHERE order_id = %s ORDER BY sku, warehouse_id
        """,
        (order_id,),
    )
    lines = tuple(OrderLine(*line) for line in await cursor.fetchall())
    id_, external_id, status, cancel_reason, reservation_id, created_at = row
    return Order(
        id=id_,
        external_id=external_id,
        status=OrderStatus(status),
        cancel_reason=cancel_reason,
        reservation_id=reservation_id,
        lines=lines,
        created_at=created_at,
    )


async def list_orders(conn: Connection, status: OrderStatus | None, limit: int) -> list[Order]:
    cursor = await conn.execute(
        """
        SELECT id FROM orders.orders
        WHERE %(status)s::text IS NULL OR status = %(status)s
        ORDER BY created_at DESC LIMIT %(limit)s
        """,
        {"status": status.value if status else None, "limit": limit},
    )
    orders = [await get_order(conn, order_id) for (order_id,) in await cursor.fetchall()]
    return [order for order in orders if order is not None]


async def update_status(
    conn: Connection,
    order_id: UUID,
    status: OrderStatus,
    *,
    cancel_reason: str | None = None,
    reservation_id: UUID | None = None,
) -> None:
    await conn.execute(
        """
        UPDATE orders.orders
        SET status = %s,
            cancel_reason = coalesce(%s, cancel_reason),
            reservation_id = coalesce(%s, reservation_id),
            updated_at = now()
        WHERE id = %s
        """,
        (status.value, cancel_reason, reservation_id, order_id),
    )


async def append_history(
    conn: Connection, order_id: UUID, status: OrderStatus, cause: str, reason: str | None
) -> None:
    await conn.execute(
        """
        INSERT INTO orders.order_history (order_id, status, reason, cause)
        VALUES (%s, %s, %s, %s)
        """,
        (order_id, status.value, reason, cause),
    )


async def get_history(conn: Connection, order_id: UUID) -> list[HistoryEntry]:
    cursor = await conn.execute(
        """
        SELECT status, reason, cause, created_at FROM orders.order_history
        WHERE order_id = %s ORDER BY id
        """,
        (order_id,),
    )
    return [
        HistoryEntry(OrderStatus(status), reason, cause, at)
        for status, reason, cause, at in await cursor.fetchall()
    ]
