from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID, uuid4

from psycopg import errors

from stockroom.inventory import repository
from stockroom.inventory.domain import (
    IdempotencyConflictError,
    InsufficientStockError,
    Reservation,
    ReservationNotFoundError,
    ReservationStatus,
    StockKey,
    StockLevel,
    UnknownStockItemsError,
    find_shortfalls,
    normalize_lines,
    should_commit,
    should_pin,
    should_release,
)
from stockroom.shared.db import Connection, Pool


@dataclass(frozen=True, slots=True)
class ReserveResult:
    reservation: Reservation
    created: bool


async def reserve(
    pool: Pool, order_id: str, lines: Iterable[tuple[StockKey, int]], *, ttl_seconds: int
) -> ReserveResult:
    async with pool.connection() as conn, conn.transaction():
        return await reserve_in(conn, order_id, lines, ttl_seconds=ttl_seconds)


async def reserve_in(
    conn: Connection, order_id: str, lines: Iterable[tuple[StockKey, int]], *, ttl_seconds: int
) -> ReserveResult:
    """Reserve every line or none, inside the caller's transaction.

    Idempotent per order: a retry with the same lines returns the first result.
    """
    requested = normalize_lines(lines)
    reservation_id = uuid4()
    if not await repository.try_insert_reservation(conn, reservation_id, order_id, ttl_seconds):
        existing = await repository.get_reservation_by_order(conn, order_id)
        assert existing is not None
        if existing.lines != requested:
            raise IdempotencyConflictError(order_id)
        return ReserveResult(existing, created=False)

    keys = list(requested)
    found = await repository.lock_stock_items(conn, keys)
    if unknown := set(keys) - found:
        raise UnknownStockItemsError(unknown)
    # Must be a separate statement from the lock: in READ COMMITTED each statement takes
    # a fresh snapshot, so this one sees reservations committed while we waited.
    available = await repository.available_quantities(conn, keys)
    if shortfalls := find_shortfalls(requested, available):
        raise InsufficientStockError(shortfalls)

    await repository.insert_reservation_lines(conn, reservation_id, requested)
    created = await repository.get_reservation(conn, reservation_id)
    assert created is not None
    return ReserveResult(created, created=True)


async def commit(pool: Pool, reservation_id: UUID) -> Reservation:
    async with pool.connection() as conn, conn.transaction():
        return await commit_in(conn, reservation_id)


async def commit_in(conn: Connection, reservation_id: UUID) -> Reservation:
    """Turn the reservation into a shipment: on-hand goes down, the hold goes away."""
    reservation = await _get_locked(conn, reservation_id)
    # Lock the stock rows before judging expiry. Otherwise a reservation could expire,
    # have its units taken by someone else, and still be committed by a transaction
    # that read the clock just before the deadline.
    await repository.lock_stock_items(conn, list(reservation.lines))
    reservation = await _get_locked(conn, reservation_id)
    if should_commit(reservation.status):
        await repository.record_shipment(conn, reservation_id)
        await repository.set_status(conn, reservation_id, ReservationStatus.COMMITTED)
    return await _get_locked(conn, reservation_id)


async def pin_in(conn: Connection, reservation_id: UUID) -> Reservation:
    """Stop the hold from expiring once the goods are picked."""
    reservation = await _get_locked(conn, reservation_id)
    # Same reasoning as commit: judge expiry only while holding the stock rows.
    await repository.lock_stock_items(conn, list(reservation.lines))
    reservation = await _get_locked(conn, reservation_id)
    if should_pin(reservation.status):
        await repository.pin(conn, reservation_id)
    return await _get_locked(conn, reservation_id)


async def release(pool: Pool, reservation_id: UUID) -> Reservation:
    async with pool.connection() as conn, conn.transaction():
        return await release_in(conn, reservation_id)


async def release_in(conn: Connection, reservation_id: UUID) -> Reservation:
    reservation = await _get_locked(conn, reservation_id)
    if should_release(reservation.status):
        await repository.set_status(conn, reservation_id, ReservationStatus.RELEASED)
    return await _get_locked(conn, reservation_id)


async def get(pool: Pool, reservation_id: UUID) -> Reservation:
    async with pool.connection() as conn:
        reservation = await repository.get_reservation(conn, reservation_id)
    if reservation is None:
        raise ReservationNotFoundError(reservation_id)
    return reservation


async def receive(pool: Pool, key: StockKey, quantity: int, reference: str | None) -> None:
    async with pool.connection() as conn:
        try:
            async with conn.transaction():
                await repository.record_receipt(conn, key, quantity, reference)
        except errors.ForeignKeyViolation as exc:
            raise UnknownStockItemsError([key]) from exc


async def stock_levels(pool: Pool, warehouse_id: str | None = None) -> list[StockLevel]:
    async with pool.connection() as conn:
        return await repository.list_stock_levels(conn, warehouse_id)


async def _get_locked(conn: Connection, reservation_id: UUID) -> Reservation:
    reservation = await repository.get_reservation(conn, reservation_id, lock=True)
    if reservation is None:
        raise ReservationNotFoundError(reservation_id)
    return reservation
