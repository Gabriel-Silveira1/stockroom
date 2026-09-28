from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


@dataclass(frozen=True, order=True, slots=True)
class StockKey:
    sku: str
    warehouse_id: str


class ReservationStatus(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    COMMITTED = "committed"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class Reservation:
    id: UUID
    order_id: str
    status: ReservationStatus
    expires_at: datetime
    lines: Mapping[StockKey, int]


@dataclass(frozen=True, slots=True)
class StockLevel:
    key: StockKey
    on_hand: int
    reserved: int
    available: int
    name: str = ""


@dataclass(frozen=True, slots=True)
class Shortfall:
    key: StockKey
    requested: int
    available: int


class InventoryError(Exception):
    pass


class InvalidReservationRequestError(InventoryError):
    pass


class UnknownStockItemsError(InventoryError):
    def __init__(self, keys: Iterable[StockKey]) -> None:
        self.keys = sorted(keys)
        super().__init__(f"unknown stock items: {self.keys}")


class InsufficientStockError(InventoryError):
    def __init__(self, shortfalls: list[Shortfall]) -> None:
        self.shortfalls = shortfalls
        super().__init__(f"insufficient stock: {shortfalls}")


class ReservationNotFoundError(InventoryError):
    def __init__(self, reservation_id: UUID) -> None:
        self.reservation_id = reservation_id
        super().__init__(f"reservation {reservation_id} not found")


class IdempotencyConflictError(InventoryError):
    """The order was already reserved with different lines: a bug upstream, not a retry."""

    def __init__(self, order_id: str) -> None:
        self.order_id = order_id
        super().__init__(f"order {order_id} was already reserved with different lines")


class InvalidTransitionError(InventoryError):
    def __init__(self, current: ReservationStatus, action: str) -> None:
        self.current = current
        self.action = action
        super().__init__(f"cannot {action} a reservation that is {current}")


def normalize_lines(lines: Iterable[tuple[StockKey, int]]) -> dict[StockKey, int]:
    """Merge repeated items and sort them.

    Sorting matters beyond tidiness: every transaction then locks stock rows in the same
    order, which rules out deadlocks between two multi-line reservations.
    """
    merged: dict[StockKey, int] = {}
    for key, quantity in lines:
        if quantity <= 0:
            raise InvalidReservationRequestError(f"quantity for {key} must be positive")
        merged[key] = merged.get(key, 0) + quantity
    if not merged:
        raise InvalidReservationRequestError("a reservation needs at least one line")
    return dict(sorted(merged.items()))


def find_shortfalls(
    requested: Mapping[StockKey, int], available: Mapping[StockKey, int]
) -> list[Shortfall]:
    return [
        Shortfall(key, quantity, available.get(key, 0))
        for key, quantity in requested.items()
        if available.get(key, 0) < quantity
    ]


def resolve_status(
    stored: ReservationStatus, expires_at: datetime, now: datetime, *, pinned: bool = False
) -> ReservationStatus:
    """Expiry is derived, never stored: an active, unpinned reservation past its TTL is
    expired."""
    if stored is ReservationStatus.ACTIVE and not pinned and expires_at <= now:
        return ReservationStatus.EXPIRED
    return stored


def should_commit(current: ReservationStatus) -> bool:
    """True when the commit must be applied; False when it already was (idempotent retry)."""
    if current is ReservationStatus.ACTIVE:
        return True
    if current is ReservationStatus.COMMITTED:
        return False
    raise InvalidTransitionError(current, "commit")


def should_release(current: ReservationStatus) -> bool:
    """True when the release must be applied; False when the stock is already free."""
    if current is ReservationStatus.ACTIVE:
        return True
    if current in (ReservationStatus.RELEASED, ReservationStatus.EXPIRED):
        return False
    raise InvalidTransitionError(current, "release")


def should_pin(current: ReservationStatus) -> bool:
    """True when the hold must stop expiring because the goods are now in a box.

    False when that no longer matters (already shipped). A hold that expired or was
    released before picking cannot be pinned: its units may already belong to someone.
    """
    if current is ReservationStatus.ACTIVE:
        return True
    if current is ReservationStatus.COMMITTED:
        return False
    raise InvalidTransitionError(current, "pin")
