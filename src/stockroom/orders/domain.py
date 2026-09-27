import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class OrderStatus(StrEnum):
    PENDING = "pending"
    RESERVED = "reserved"
    PICKED = "picked"
    SHIPPED = "shipped"
    CANCELLED = "cancelled"


_ALLOWED: Mapping[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING: frozenset({OrderStatus.RESERVED, OrderStatus.CANCELLED}),
    OrderStatus.RESERVED: frozenset({OrderStatus.PICKED, OrderStatus.CANCELLED}),
    OrderStatus.PICKED: frozenset({OrderStatus.SHIPPED, OrderStatus.CANCELLED}),
    OrderStatus.SHIPPED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class OrderLine:
    sku: str
    warehouse_id: str
    quantity: int


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    status: OrderStatus
    reason: str | None
    cause: str
    at: datetime


@dataclass(frozen=True, slots=True)
class Order:
    id: UUID
    external_id: str
    status: OrderStatus
    cancel_reason: str | None
    reservation_id: UUID | None
    tracking_number: str | None
    lines: tuple[OrderLine, ...]
    created_at: datetime


class OrdersError(Exception):
    pass


class OrderNotFoundError(OrdersError):
    def __init__(self, order_id: UUID) -> None:
        self.order_id = order_id
        super().__init__(f"order {order_id} not found")


class InvalidOrderTransitionError(OrdersError):
    def __init__(self, current: OrderStatus, target: OrderStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(f"cannot move an order from {current} to {target}")


class IdempotencyKeyReusedError(OrdersError):
    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"idempotency key {key!r} was already used with a different payload")


def should_transition(current: OrderStatus, target: OrderStatus) -> bool:
    """True when the move must be applied; False when the order is already there.

    Events arrive at least once, so reaching the same status twice is normal and a no-op.
    """
    if current is target:
        return False
    if target in _ALLOWED[current]:
        return True
    raise InvalidOrderTransitionError(current, target)


def request_hash(payload: Mapping[str, Any]) -> str:
    """Stable fingerprint of a webhook body, independent of key order and whitespace."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
