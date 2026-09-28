from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from stockroom.orders import repository
from stockroom.orders.domain import (
    HistoryEntry,
    IdempotencyKeyReusedError,
    Order,
    OrderLine,
    OrderNotFoundError,
    OrderStatus,
    request_hash,
    should_transition,
)
from stockroom.shared.db import Connection, Pool
from stockroom.shared.events import Event
from stockroom.shared.outbox import enqueue

SCHEMA = "orders"


@dataclass(frozen=True, slots=True)
class PlaceResult:
    order: Order
    status_code: int
    replayed: bool


async def place_order(
    pool: Pool,
    *,
    idempotency_key: str,
    payload: Mapping[str, Any],
    external_id: str,
    lines: Iterable[OrderLine],
) -> PlaceResult:
    """Create the order and its `order.placed` event in one transaction.

    Two layers of deduplication: the Idempotency-Key replays the first response, and the
    unique external id catches the same order arriving under a different key.
    """
    order_id = uuid4()
    fingerprint = request_hash(payload)
    async with pool.connection() as conn, conn.transaction():
        if not await repository.claim_idempotency_key(conn, idempotency_key, fingerprint, order_id):
            stored_hash, existing_id, status_code = await repository.get_idempotency_record(
                conn, idempotency_key
            )
            if stored_hash != fingerprint:
                raise IdempotencyKeyReusedError(idempotency_key)
            return PlaceResult(await _get(conn, existing_id), status_code, replayed=True)

        merged = merge_lines(lines)
        if not await repository.try_insert_order(conn, order_id, external_id, merged):
            existing_id = await repository.get_order_id_by_external(conn, external_id)
            await repository.point_idempotency_key(conn, idempotency_key, existing_id, 200)
            return PlaceResult(await _get(conn, existing_id), 200, replayed=True)

        await repository.append_history(conn, order_id, OrderStatus.PENDING, "webhook", None)
        await enqueue(
            conn,
            SCHEMA,
            Event(
                "order.placed",
                {
                    "order_id": str(order_id),
                    "lines": [
                        {"sku": ln.sku, "warehouse_id": ln.warehouse_id, "quantity": ln.quantity}
                        for ln in merged
                    ],
                },
            ),
        )
        return PlaceResult(await _get(conn, order_id), 201, replayed=False)


def merge_lines(lines: Iterable[OrderLine]) -> list[OrderLine]:
    totals: dict[tuple[str, str], int] = {}
    for line in lines:
        key = (line.sku, line.warehouse_id)
        totals[key] = totals.get(key, 0) + line.quantity
    return [OrderLine(sku, wh, qty) for (sku, wh), qty in sorted(totals.items())]


async def transition_in(
    conn: Connection,
    order_id: UUID,
    target: OrderStatus,
    *,
    cause: str,
    reason: str | None = None,
    reservation_id: UUID | None = None,
    tracking_number: str | None = None,
) -> bool:
    """Move the order inside the caller's transaction. False when it was already there."""
    order = await repository.get_order(conn, order_id, lock=True)
    if order is None:
        raise OrderNotFoundError(order_id)
    if not should_transition(order.status, target):
        return False
    await repository.update_status(
        conn,
        order_id,
        target,
        cancel_reason=reason if target is OrderStatus.CANCELLED else None,
        reservation_id=reservation_id,
        tracking_number=tracking_number,
    )
    await repository.append_history(conn, order_id, target, cause, reason)
    return True


async def cancel_in(conn: Connection, order_id: UUID, *, cause: str, reason: str) -> bool:
    """Cancel and announce it, so inventory releases whatever it holds for the order."""
    if not await transition_in(conn, order_id, OrderStatus.CANCELLED, cause=cause, reason=reason):
        return False
    order = await _get(conn, order_id)
    await announce_cancellation(conn, order_id, reason, order.reservation_id)
    return True


async def announce_cancellation(
    conn: Connection, order_id: UUID, reason: str, reservation_id: UUID | None
) -> None:
    """`reservation_id` is null when the order never got stock, so nothing downstream can
    be holding or shipping anything for it."""
    await enqueue(
        conn,
        SCHEMA,
        Event(
            "order.cancelled",
            {
                "order_id": str(order_id),
                "reason": reason,
                "reservation_id": str(reservation_id) if reservation_id else None,
            },
        ),
    )


async def cancel(pool: Pool, order_id: UUID) -> Order:
    async with pool.connection() as conn, conn.transaction():
        await cancel_in(conn, order_id, cause="api", reason="cancelled_by_merchant")
        return await _get(conn, order_id)


async def get(pool: Pool, order_id: UUID) -> tuple[Order, list[HistoryEntry]]:
    async with pool.connection() as conn:
        order = await _get(conn, order_id)
        return order, await repository.get_history(conn, order_id)


async def list_recent(pool: Pool, status: OrderStatus | None, limit: int) -> list[Order]:
    async with pool.connection() as conn:
        return await repository.list_orders(conn, status, limit)


async def _get(conn: Connection, order_id: UUID) -> Order:
    order = await repository.get_order(conn, order_id)
    if order is None:
        raise OrderNotFoundError(order_id)
    return order
