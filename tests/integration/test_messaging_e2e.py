"""Relay, topology, consumer, retry and dead-lettering against a real RabbitMQ."""

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from uuid import uuid4

import aio_pika
import pytest

from stockroom.inventory import handlers as inventory_handlers
from stockroom.inventory import service as inventory
from stockroom.orders import handlers as orders_handlers
from stockroom.orders import service as orders
from stockroom.orders.domain import OrderLine, OrderStatus
from stockroom.shared.db import Connection, Pool
from stockroom.shared.events import Event
from stockroom.shared.outbox import to_message
from stockroom.shared.runtime import messaging
from stockroom.shared.settings import ServiceSettings
from tests.integration.conftest import CORE_WEST

RABBITMQ_URL = os.environ.get(
    "STOCKROOM_TEST_RABBITMQ_URL", "amqp://stockroom:stockroom@localhost:5672/"
)


@pytest.fixture
async def settings() -> AsyncIterator[ServiceSettings]:
    """Private exchange and queues per test, so a running stack never steals the messages."""
    run = uuid4().hex[:8]
    settings = ServiceSettings(
        rabbitmq_url=RABBITMQ_URL,
        exchange_name=f"test-{run}.events",
        queue_prefix=f"test-{run}.",
        relay_idle_seconds=0.02,
        retry_delay_ms=100,
        max_delivery_attempts=3,
    )
    yield settings
    connection = await aio_pika.connect(RABBITMQ_URL)
    async with connection:
        channel = await connection.channel()
        for schema in ("inventory", "orders"):
            for suffix in ("", ".retry", ".dlq"):
                await channel.queue_delete(f"{settings.queue_prefix}{schema}{suffix}")
        await channel.exchange_delete(settings.exchange_name)


async def _eventually(check: Callable[[], Awaitable[bool]]) -> None:
    """Poll state owned by Postgres or the broker for up to ten seconds."""
    for _ in range(200):
        if await check():
            return
        await asyncio.sleep(0.05)
    pytest.fail("condition not reached within 10s")


async def test_order_is_reserved_through_the_broker(pool: Pool, settings: ServiceSettings) -> None:
    await inventory.receive(pool, CORE_WEST, 5, "test")
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(
            messaging(
                pool,
                settings,
                schema="inventory",
                queue_name=settings.queue_prefix + "inventory",
                routing_keys=inventory_handlers.ROUTING_KEYS,
                handlers=inventory_handlers.build_handlers(ttl_seconds=900),
            )
        )
        await stack.enter_async_context(
            messaging(
                pool,
                settings,
                schema="orders",
                queue_name=settings.queue_prefix + "orders",
                routing_keys=orders_handlers.ROUTING_KEYS,
                handlers=orders_handlers.HANDLERS,
            )
        )
        placed = await orders.place_order(
            pool,
            idempotency_key="e2e",
            payload={"id": "e2e"},
            external_id="e2e",
            lines=[OrderLine("CORE-001", "eu-west", 2)],
        )

        async def reserved() -> bool:
            order, _ = await orders.get(pool, placed.order.id)
            return order.status is OrderStatus.RESERVED

        await _eventually(reserved)


async def test_failing_handler_is_retried_then_dead_lettered(
    pool: Pool, settings: ServiceSettings
) -> None:
    attempts = 0

    async def always_fails(conn: Connection, event: Event) -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("downstream is down")

    queue_name = settings.queue_prefix + "inventory"
    async with messaging(
        pool,
        settings,
        schema="inventory",
        queue_name=queue_name,
        routing_keys=("order.placed",),
        handlers={"order.placed": always_fails},
    ):
        await _publish_raw(settings, Event("order.placed", {"order_id": "x"}))

        async def dead_lettered() -> bool:
            return await _queue_depth(settings, queue_name + ".dlq") == 1

        await _eventually(dead_lettered)

    assert attempts == settings.max_delivery_attempts


async def test_transient_failure_is_retried_and_handled_once(
    pool: Pool, settings: ServiceSettings
) -> None:
    calls = 0

    async def fails_once(conn: Connection, event: Event) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("blip")

    queue_name = settings.queue_prefix + "inventory"
    async with messaging(
        pool,
        settings,
        schema="inventory",
        queue_name=queue_name,
        routing_keys=("order.placed",),
        handlers={"order.placed": fails_once},
    ):
        event = Event("order.placed", {"order_id": "x"})
        await _publish_raw(settings, event)
        await _publish_raw(settings, event)

        async def handled() -> bool:
            return calls >= 2

        await _eventually(handled)
        await asyncio.sleep(0.5)

    assert calls == 2
    assert await _queue_depth(settings, queue_name + ".dlq") == 0


async def _publish_raw(settings: ServiceSettings, event: Event) -> None:
    connection = await aio_pika.connect(settings.rabbitmq_url)
    async with connection:
        channel = await connection.channel()
        exchange = await channel.get_exchange(settings.exchange_name)
        await exchange.publish(to_message(event), routing_key=event.type)


async def _queue_depth(settings: ServiceSettings, queue_name: str) -> int:
    connection = await aio_pika.connect(settings.rabbitmq_url)
    async with connection:
        channel = await connection.channel()
        queue = await channel.declare_queue(queue_name, passive=True)
        return queue.declaration_result.message_count or 0
