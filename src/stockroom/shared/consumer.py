"""Idempotent event consumer with delayed retries and a dead-letter queue.

Each service owns one queue. A message is handled inside a database transaction that
first records its event id in `processed_messages`; a redelivered event finds its id
already there and is skipped. The ack is sent only after that transaction commits, so
a crash in between means redelivery, never loss.

A failing message is not requeued in a hot loop. It is parked in `<queue>.retry`, whose
TTL dead-letters it back to `<queue>` after a delay. After the last attempt it goes to
`<queue>.dlq` for a human to inspect.
"""

import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping

from aio_pika import DeliveryMode, ExchangeType, Message
from aio_pika.abc import (
    AbstractChannel,
    AbstractExchange,
    AbstractIncomingMessage,
    AbstractQueue,
)
from psycopg import sql

from stockroom.shared.db import Connection, Pool
from stockroom.shared.events import Event, MalformedEventError, decode

ATTEMPT_HEADER = "x-attempt"

type Handler = Callable[[Connection, Event], Awaitable[None]]

log = logging.getLogger(__name__)


async def declare_topology(
    channel: AbstractChannel,
    queue_name: str,
    routing_keys: Iterable[str],
    *,
    exchange_name: str,
    retry_delay_ms: int,
) -> tuple[AbstractExchange, AbstractQueue]:
    exchange = await channel.declare_exchange(exchange_name, ExchangeType.TOPIC, durable=True)
    queue = await channel.declare_queue(queue_name, durable=True)
    for routing_key in routing_keys:
        await queue.bind(exchange, routing_key)
    await channel.declare_queue(
        f"{queue_name}.retry",
        durable=True,
        arguments={
            "x-message-ttl": retry_delay_ms,
            "x-dead-letter-exchange": "",
            "x-dead-letter-routing-key": queue_name,
        },
    )
    await channel.declare_queue(f"{queue_name}.dlq", durable=True)
    return exchange, queue


async def process(pool: Pool, schema: str, handlers: Mapping[str, Handler], event: Event) -> bool:
    """Run the handler once per event id. Returns False when the event was a duplicate."""
    handler = handlers.get(event.type)
    if handler is None:
        return False
    async with pool.connection() as conn, conn.transaction():
        cursor = await conn.execute(
            sql.SQL(
                "INSERT INTO {}.processed_messages (message_id, type) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING RETURNING message_id"
            ).format(sql.Identifier(schema)),
            (event.id, event.type),
        )
        if await cursor.fetchone() is None:
            return False
        await handler(conn, event)
    return True


class Consumer:
    def __init__(
        self,
        pool: Pool,
        channel: AbstractChannel,
        *,
        schema: str,
        queue_name: str,
        handlers: Mapping[str, Handler],
        max_attempts: int,
    ) -> None:
        self._pool = pool
        self._channel = channel
        self._schema = schema
        self._queue_name = queue_name
        self._handlers = handlers
        self._max_attempts = max_attempts

    async def on_message(self, message: AbstractIncomingMessage) -> None:
        try:
            event = decode(message.body)
        except MalformedEventError:
            log.exception("malformed message on %s; dead-lettering", self._queue_name)
            await self._park(message, f"{self._queue_name}.dlq", attempt=1)
            return
        try:
            await process(self._pool, self._schema, self._handlers, event)
        except Exception:
            await self._retry_or_dead_letter(message, event)
            return
        await message.ack()

    async def _retry_or_dead_letter(self, message: AbstractIncomingMessage, event: Event) -> None:
        attempt = int(str(message.headers.get(ATTEMPT_HEADER, 1)))
        if attempt >= self._max_attempts:
            log.exception("%s %s failed %d times; dead-lettering", event.type, event.id, attempt)
            await self._park(message, f"{self._queue_name}.dlq", attempt)
        else:
            log.warning("%s %s failed (attempt %d); retrying", event.type, event.id, attempt)
            await self._park(message, f"{self._queue_name}.retry", attempt + 1)

    async def _park(self, message: AbstractIncomingMessage, queue: str, attempt: int) -> None:
        # Publish-then-ack is not atomic. A crash in between duplicates the message,
        # which the processed_messages check absorbs.
        await self._channel.default_exchange.publish(
            Message(
                message.body,
                headers={**message.headers, ATTEMPT_HEADER: attempt},
                message_id=message.message_id,
                type=message.type,
                content_type=message.content_type,
                delivery_mode=DeliveryMode.PERSISTENT,
            ),
            routing_key=queue,
        )
        await message.ack()
