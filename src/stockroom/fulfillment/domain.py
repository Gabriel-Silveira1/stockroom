from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID


class ShipmentStatus(StrEnum):
    PICKED = "picked"
    SHIPPED = "shipped"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ShipmentLine:
    sku: str
    warehouse_id: str
    quantity: int


@dataclass(frozen=True, slots=True)
class Shipment:
    id: UUID
    order_id: UUID
    reservation_id: UUID | None
    status: ShipmentStatus
    attempts: int
    next_attempt_at: datetime
    last_error: str | None
    tracking_number: str | None
    lines: tuple[ShipmentLine, ...]


class DispatchOutcome(StrEnum):
    SHIPPED = "shipped"
    RETRY = "retry"
    FAILED = "failed"


def retry_delay(attempt: int, *, base: timedelta, cap: timedelta) -> timedelta:
    """Exponential backoff: base, 2x base, 4x base... never above cap."""
    delay: timedelta = base * 2 ** max(attempt - 1, 0)
    return min(delay, cap)


def after_failure(attempts_so_far: int, *, max_attempts: int, permanent: bool) -> DispatchOutcome:
    """What a failed carrier call means for the shipment, counting the call just made."""
    if permanent or attempts_so_far + 1 >= max_attempts:
        return DispatchOutcome.FAILED
    return DispatchOutcome.RETRY


def can_cancel(status: ShipmentStatus) -> bool:
    """Only a box that has not left can be stopped; anything else is already final."""
    return status is ShipmentStatus.PICKED
