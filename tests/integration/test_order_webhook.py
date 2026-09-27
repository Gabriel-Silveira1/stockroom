import asyncio
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from stockroom.orders.api import create_app, get_pool
from stockroom.orders.settings import OrdersSettings
from stockroom.shared.db import Pool
from tests.integration.bus import drain


def _webhook(order_id: int = 1001, quantity: int = 2) -> dict[str, Any]:
    return {
        "id": order_id,
        "line_items": [{"sku": "CORE-001", "quantity": quantity}],
        "note": "fields the service does not need are ignored",
    }


@pytest.fixture
async def client(pool: Pool) -> Any:
    app = create_app(OrdersSettings())
    app.dependency_overrides[get_pool] = lambda: pool
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def _order_count(pool: Pool) -> int:
    async with pool.connection() as conn:
        cursor = await conn.execute("SELECT count(*) FROM orders.orders")
        row = await cursor.fetchone()
    assert row is not None
    count: int = row[0]
    return count


async def test_same_webhook_three_times_creates_one_order(client: AsyncClient, pool: Pool) -> None:
    headers = {"Idempotency-Key": "wh-1001"}

    responses = [
        await client.post("/webhooks/orders", json=_webhook(), headers=headers) for _ in range(3)
    ]

    assert [r.status_code for r in responses] == [201, 201, 201]
    assert [r.headers.get("Idempotent-Replayed") for r in responses] == [None, "true", "true"]
    assert len({r.json()["id"] for r in responses}) == 1
    assert await _order_count(pool) == 1
    assert [e.type for e in await drain(pool, "orders")] == ["order.placed"]


async def test_concurrent_deliveries_of_one_webhook_create_one_order(
    client: AsyncClient, pool: Pool
) -> None:
    headers = {"Idempotency-Key": "wh-1001"}

    responses = await asyncio.gather(
        *(client.post("/webhooks/orders", json=_webhook(), headers=headers) for _ in range(10))
    )

    assert len({r.json()["id"] for r in responses}) == 1
    assert await _order_count(pool) == 1
    assert len(await drain(pool, "orders")) == 1


async def test_reusing_a_key_for_a_different_order_is_rejected(
    client: AsyncClient, pool: Pool
) -> None:
    headers = {"Idempotency-Key": "wh-1001"}
    await client.post("/webhooks/orders", json=_webhook(quantity=2), headers=headers)

    response = await client.post("/webhooks/orders", json=_webhook(quantity=5), headers=headers)

    assert response.status_code == 422
    assert response.json()["error"] == "idempotency_key_reused"


async def test_same_order_under_a_new_key_returns_the_existing_order(
    client: AsyncClient, pool: Pool
) -> None:
    first = await client.post("/webhooks/orders", json=_webhook(), headers={"Idempotency-Key": "a"})

    second = await client.post(
        "/webhooks/orders", json=_webhook(), headers={"Idempotency-Key": "b"}
    )

    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert await _order_count(pool) == 1


async def test_webhook_without_idempotency_key_is_rejected(client: AsyncClient) -> None:
    response = await client.post("/webhooks/orders", json=_webhook())

    assert response.status_code == 422


async def test_repeated_skus_are_merged_into_one_line(client: AsyncClient) -> None:
    body = {
        "id": 7,
        "line_items": [{"sku": "CORE-001", "quantity": 1}, {"sku": "CORE-001", "quantity": 2}],
    }

    response = await client.post("/webhooks/orders", json=body, headers={"Idempotency-Key": "k"})

    assert response.json()["lines"] == [
        {"sku": "CORE-001", "warehouse_id": "eu-west", "quantity": 3}
    ]
