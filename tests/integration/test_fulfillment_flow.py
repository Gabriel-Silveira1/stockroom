from collections.abc import Sequence
from datetime import timedelta
from uuid import UUID

import pytest

from stockroom.fulfillment import service as fulfillment
from stockroom.fulfillment.carrier import CarrierError
from stockroom.fulfillment.domain import ShipmentLine, ShipmentStatus
from stockroom.inventory import service as inventory
from stockroom.inventory.domain import ReservationStatus
from stockroom.orders import service as orders
from stockroom.orders.domain import OrderLine, OrderStatus
from stockroom.shared.db import Pool
from tests.integration.bus import pump
from tests.integration.conftest import CORE_WEST

MAX_ATTEMPTS = 3


class FakeCarrier:
    """Fails the first `failures` calls, then books. Idempotent per shipment, like the
    real one: a repeated booking returns the first tracking number."""

    def __init__(self, failures: int = 0, *, permanent: bool = False) -> None:
        self.failures = failures
        self.permanent = permanent
        self.calls: list[UUID] = []
        self.bookings: dict[UUID, str] = {}

    async def book(self, shipment_id: UUID, order_id: UUID, lines: Sequence[ShipmentLine]) -> str:
        self.calls.append(shipment_id)
        if len(self.calls) <= self.failures:
            raise CarrierError("carrier down", permanent=self.permanent)
        return self.bookings.setdefault(shipment_id, f"TRK-{len(self.bookings) + 1}")


def _dispatcher(pool: Pool, carrier: FakeCarrier) -> fulfillment.Dispatcher:
    return fulfillment.Dispatcher(
        pool, carrier, max_attempts=MAX_ATTEMPTS, base_delay=timedelta(0), max_delay=timedelta(0)
    )


async def _reserved_order(pool: Pool) -> UUID:
    await inventory.receive(pool, CORE_WEST, 5, "test")
    result = await orders.place_order(
        pool,
        idempotency_key="k-1",
        payload={"id": "1"},
        external_id="1",
        lines=[OrderLine("CORE-001", "eu-west", 2)],
    )
    await pump(pool)
    return result.order.id


async def _dispatch_all(dispatcher: fulfillment.Dispatcher) -> None:
    while await dispatcher.dispatch_next():
        pass


async def _stock(pool: Pool) -> tuple[int, int, int]:
    [level] = [lvl for lvl in await inventory.stock_levels(pool) if lvl.key == CORE_WEST]
    return level.on_hand, level.reserved, level.available


async def test_reserved_order_is_picked_and_shipped(pool: Pool) -> None:
    order_id = await _reserved_order(pool)
    carrier = FakeCarrier()

    await _dispatch_all(_dispatcher(pool, carrier))
    await pump(pool)

    order, history = await orders.get(pool, order_id)
    assert order.status is OrderStatus.SHIPPED
    assert order.tracking_number == "TRK-1"
    assert [h.status for h in history] == [
        OrderStatus.PENDING,
        OrderStatus.RESERVED,
        OrderStatus.PICKED,
        OrderStatus.SHIPPED,
    ]
    assert await _stock(pool) == (3, 0, 3)


async def test_transient_carrier_failures_are_retried_with_the_same_key(pool: Pool) -> None:
    order_id = await _reserved_order(pool)
    carrier = FakeCarrier(failures=MAX_ATTEMPTS - 1)

    await _dispatch_all(_dispatcher(pool, carrier))
    await pump(pool)

    shipment = await fulfillment.get(pool, order_id)
    assert shipment is not None
    assert shipment.status is ShipmentStatus.SHIPPED
    assert shipment.attempts == MAX_ATTEMPTS
    assert set(carrier.calls) == {shipment.id}
    assert (await orders.get(pool, order_id))[0].status is OrderStatus.SHIPPED


@pytest.mark.parametrize(("failures", "permanent"), [(MAX_ATTEMPTS, False), (1, True)])
async def test_carrier_giving_up_cancels_the_order_and_releases_stock(
    pool: Pool, failures: int, permanent: bool
) -> None:
    order_id = await _reserved_order(pool)
    carrier = FakeCarrier(failures=failures, permanent=permanent)

    await _dispatch_all(_dispatcher(pool, carrier))
    sent = await pump(pool)

    order, _ = await orders.get(pool, order_id)
    shipment = await fulfillment.get(pool, order_id)
    assert shipment is not None
    assert shipment.status is ShipmentStatus.FAILED
    assert order.status is OrderStatus.CANCELLED
    assert order.cancel_reason == "carrier_failed"
    assert [e.type for e in sent] == ["fulfillment.failed", "order.cancelled", "stock.released"]
    assert await _stock(pool) == (5, 0, 5)


async def test_retry_waits_for_its_backoff(pool: Pool) -> None:
    await _reserved_order(pool)
    carrier = FakeCarrier(failures=1)
    dispatcher = fulfillment.Dispatcher(
        pool, carrier, max_attempts=3, base_delay=timedelta(hours=1), max_delay=timedelta(hours=1)
    )

    assert await dispatcher.dispatch_next() is True
    assert await dispatcher.dispatch_next() is False
    assert len(carrier.calls) == 1


async def test_cancelling_a_picked_order_stops_the_shipment(pool: Pool) -> None:
    order_id = await _reserved_order(pool)

    await orders.cancel(pool, order_id)
    await pump(pool)
    carrier = FakeCarrier()
    await _dispatch_all(_dispatcher(pool, carrier))

    shipment = await fulfillment.get(pool, order_id)
    assert shipment is not None
    assert shipment.status is ShipmentStatus.CANCELLED
    assert carrier.calls == []
    assert await _stock(pool) == (5, 0, 5)


async def test_picked_order_keeps_its_stock_after_the_ttl(pool: Pool) -> None:
    order_id = await _reserved_order(pool)
    async with pool.connection() as conn:
        await conn.execute("UPDATE inventory.reservations SET expires_at = now()")

    order, _ = await orders.get(pool, order_id)
    assert order.reservation_id is not None
    reservation = await inventory.get(pool, order.reservation_id)

    assert order.status is OrderStatus.PICKED
    assert reservation.status is ReservationStatus.ACTIVE
    assert await _stock(pool) == (5, 2, 3)
