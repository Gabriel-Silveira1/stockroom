import random
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from stockroom.carrier.api import CarrierSettings, create_app
from stockroom.fulfillment.carrier import CarrierError, HttpCarrier
from stockroom.fulfillment.domain import ShipmentLine

LINES = [ShipmentLine("CORE-001", "eu-west", 1)]


def _client(failure_rate: float) -> AsyncClient:
    app = create_app(CarrierSettings(failure_rate=failure_rate, latency_ms=0), random.Random(7))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://carrier")


async def test_booking_is_idempotent_per_shipment() -> None:
    async with _client(failure_rate=0) as client:
        carrier = HttpCarrier(client)
        shipment_id = uuid4()

        first = await carrier.book(shipment_id, uuid4(), LINES)
        second = await carrier.book(shipment_id, uuid4(), LINES)

    assert first == second
    assert first.startswith("TRK")


async def test_outage_is_a_retryable_error() -> None:
    async with _client(failure_rate=1) as client:
        with pytest.raises(CarrierError) as error:
            await HttpCarrier(client).book(uuid4(), uuid4(), LINES)

    assert error.value.permanent is False


async def test_failure_rate_can_be_changed_live() -> None:
    async with _client(failure_rate=1) as client:
        await client.put("/admin/behaviour", json={"failure_rate": 0, "latency_ms": 0})

        tracking = await HttpCarrier(client).book(uuid4(), uuid4(), LINES)

    assert tracking.startswith("TRK")
