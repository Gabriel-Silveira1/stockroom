from uuid import uuid4

import psycopg
import pytest
from httpx import ASGITransport, AsyncClient

from stockroom.inventory import service
from stockroom.inventory.api import create_app, get_pool
from stockroom.inventory.domain import (
    IdempotencyConflictError,
    InsufficientStockError,
    InvalidTransitionError,
    ReservationNotFoundError,
    ReservationStatus,
    StockKey,
    UnknownStockItemsError,
)
from stockroom.inventory.settings import InventorySettings
from stockroom.shared.db import Pool
from tests.integration.conftest import CORE_WEST, EXP_WEST

TTL = 900


async def _levels(pool: Pool, sku: str) -> tuple[int, int, int]:
    [level] = [lvl for lvl in await service.stock_levels(pool) if lvl.key.sku == sku]
    return level.on_hand, level.reserved, level.available


async def test_reservation_is_all_or_nothing(pool: Pool) -> None:
    await service.receive(pool, CORE_WEST, 5, "test")
    await service.receive(pool, EXP_WEST, 1, "test")

    with pytest.raises(InsufficientStockError) as error:
        await service.reserve(pool, "order-1", [(CORE_WEST, 2), (EXP_WEST, 2)], ttl_seconds=TTL)

    assert [s.key for s in error.value.shortfalls] == [EXP_WEST]
    assert await _levels(pool, "CORE-001") == (5, 0, 5)


async def test_same_order_with_different_lines_is_a_conflict_not_a_retry(pool: Pool) -> None:
    await service.receive(pool, CORE_WEST, 5, "test")
    await service.reserve(pool, "order-1", [(CORE_WEST, 1)], ttl_seconds=TTL)

    with pytest.raises(IdempotencyConflictError):
        await service.reserve(pool, "order-1", [(CORE_WEST, 2)], ttl_seconds=TTL)

    assert await _levels(pool, "CORE-001") == (5, 1, 4)


async def test_unknown_item_is_rejected(pool: Pool) -> None:
    with pytest.raises(UnknownStockItemsError):
        await service.reserve(pool, "order-1", [(StockKey("NOPE", "eu-west"), 1)], ttl_seconds=TTL)


async def test_commit_turns_the_hold_into_a_shipment(pool: Pool) -> None:
    await service.receive(pool, CORE_WEST, 5, "test")
    result = await service.reserve(pool, "order-1", [(CORE_WEST, 2)], ttl_seconds=TTL)

    committed = await service.commit(pool, result.reservation.id)
    retried = await service.commit(pool, result.reservation.id)

    assert committed.status is ReservationStatus.COMMITTED
    assert retried.status is ReservationStatus.COMMITTED
    assert await _levels(pool, "CORE-001") == (3, 0, 3)


async def test_release_frees_the_stock(pool: Pool) -> None:
    await service.receive(pool, CORE_WEST, 5, "test")
    result = await service.reserve(pool, "order-1", [(CORE_WEST, 2)], ttl_seconds=TTL)

    released = await service.release(pool, result.reservation.id)

    assert released.status is ReservationStatus.RELEASED
    assert await _levels(pool, "CORE-001") == (5, 0, 5)
    with pytest.raises(InvalidTransitionError):
        await service.commit(pool, result.reservation.id)


async def test_expired_reservation_stops_holding_stock_and_cannot_ship(pool: Pool) -> None:
    await service.receive(pool, CORE_WEST, 1, "test")
    result = await service.reserve(pool, "order-1", [(CORE_WEST, 1)], ttl_seconds=0)

    reservation = await service.get(pool, result.reservation.id)
    second = await service.reserve(pool, "order-2", [(CORE_WEST, 1)], ttl_seconds=TTL)

    assert reservation.status is ReservationStatus.EXPIRED
    assert second.created
    with pytest.raises(InvalidTransitionError):
        await service.commit(pool, result.reservation.id)


async def test_missing_reservation_is_reported(pool: Pool) -> None:
    with pytest.raises(ReservationNotFoundError):
        await service.get(pool, uuid4())


async def test_ledger_rejects_edits(pool: Pool) -> None:
    await service.receive(pool, CORE_WEST, 5, "test")

    async with pool.connection() as conn:
        with pytest.raises(psycopg.errors.RaiseException):
            await conn.execute("UPDATE inventory.stock_movements SET quantity = 500")


async def test_api_maps_outcomes_to_status_codes(pool: Pool) -> None:
    await service.receive(pool, CORE_WEST, 1, "test")
    app = create_app(InventorySettings())
    app.dependency_overrides[get_pool] = lambda: pool
    body = {
        "order_id": "order-1",
        "lines": [{"sku": "CORE-001", "warehouse_id": "eu-west", "quantity": 1}],
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/reservations", json=body)
        repeated = await client.post("/reservations", json=body)
        rejected = await client.post("/reservations", json={**body, "order_id": "order-2"})

    assert created.status_code == 201
    assert repeated.status_code == 200
    assert repeated.json()["id"] == created.json()["id"]
    assert rejected.status_code == 409
    assert rejected.json()["error"] == "insufficient_stock"
