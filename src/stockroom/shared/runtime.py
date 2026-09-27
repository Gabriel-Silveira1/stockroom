import asyncio
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager, suppress

import aio_pika

from stockroom.shared.consumer import Consumer, Handler, declare_topology
from stockroom.shared.db import Pool, create_pool
from stockroom.shared.migrations import apply_migrations
from stockroom.shared.outbox import OutboxRelay
from stockroom.shared.settings import ServiceSettings


@asynccontextmanager
async def messaging(
    pool: Pool,
    settings: ServiceSettings,
    *,
    schema: str,
    queue_name: str,
    routing_keys: Iterable[str],
    handlers: Mapping[str, Handler],
) -> AsyncIterator[None]:
    """Run the service's outbox relay and queue consumer for as long as the context is open.

    `connect_robust` reconnects and redeclares on its own, so a broker restart does not
    need a service restart.
    """
    connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    try:
        consumer_channel = await connection.channel()
        await consumer_channel.set_qos(prefetch_count=20)
        exchange, queue = await declare_topology(
            consumer_channel,
            queue_name,
            routing_keys,
            exchange_name=settings.exchange_name,
            retry_delay_ms=settings.retry_delay_ms,
        )
        relay_channel = await connection.channel(publisher_confirms=True)
        relay_exchange = await relay_channel.get_exchange(exchange.name)
        relay = OutboxRelay(pool, relay_exchange, schema, idle_seconds=settings.relay_idle_seconds)
        consumer = Consumer(
            pool,
            consumer_channel,
            schema=schema,
            queue_name=queue_name,
            handlers=handlers,
            max_attempts=settings.max_delivery_attempts,
        )
        relay_task = asyncio.create_task(relay.run(), name=f"{schema}-outbox-relay")
        await queue.consume(consumer.on_message)
        try:
            yield
        finally:
            relay_task.cancel()
            with suppress(asyncio.CancelledError):
                await relay_task
    finally:
        await connection.close()


@asynccontextmanager
async def run_service(
    settings: ServiceSettings,
    *,
    schema: str,
    routing_keys: Iterable[str],
    handlers: Mapping[str, Handler],
) -> AsyncIterator[Pool]:
    """Open the pool, apply the service's migrations, then keep messaging running."""
    async with create_pool(settings.database_url, max_size=settings.db_pool_max_size) as pool:
        async with pool.connection() as conn:
            await apply_migrations(conn, service=schema, package=f"stockroom.{schema}.migrations")
        async with messaging(
            pool,
            settings,
            schema=schema,
            queue_name=settings.queue_prefix + schema,
            routing_keys=routing_keys,
            handlers=handlers,
        ):
            yield pool
