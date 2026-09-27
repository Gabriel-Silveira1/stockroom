"""Transactional outbox.

A service never publishes to the broker inside a business transaction. It writes the
event to its own `outbox` table in that same transaction, so the state change and the
event are committed together or not at all. The relay publishes afterwards.

Delivery is at-least-once: if the relay dies after publishing and before marking the
row, the event goes out again. Consumers deduplicate by event id (see `consumer`).
"""

import asyncio
import logging

from aio_pika import DeliveryMode, Message
from aio_pika.abc import AbstractExchange
from psycopg import sql
from psycopg.types.json import Jsonb

from stockroom.shared.db import Connection, Pool
from stockroom.shared.events import Event, encode

log = logging.getLogger(__name__)


async def enqueue(conn: Connection, schema: str, event: Event) -> None:
    await conn.execute(
        sql.SQL(
            "INSERT INTO {}.outbox (id, type, data, occurred_at) VALUES (%s, %s, %s, %s)"
        ).format(sql.Identifier(schema)),
        (event.id, event.type, Jsonb(event.data), event.occurred_at),
    )


class OutboxRelay:
    def __init__(
        self,
        pool: Pool,
        exchange: AbstractExchange,
        schema: str,
        *,
        batch_size: int = 100,
        idle_seconds: float = 0.2,
    ) -> None:
        self._pool = pool
        self._exchange = exchange
        self._schema = sql.Identifier(schema)
        self._batch_size = batch_size
        self._idle_seconds = idle_seconds

    async def publish_pending(self) -> int:
        """Publish one batch in position order and mark it sent. Returns how many were sent."""
        async with self._pool.connection() as conn, conn.transaction():
            # SKIP LOCKED lets several relay replicas share the table without double work.
            cursor = await conn.execute(
                sql.SQL(
                    "SELECT id, type, data, occurred_at FROM {}.outbox "
                    "WHERE published_at IS NULL ORDER BY position LIMIT %s "
                    "FOR UPDATE SKIP LOCKED"
                ).format(self._schema),
                (self._batch_size,),
            )
            rows = await cursor.fetchall()
            for event_id, event_type, data, occurred_at in rows:
                event = Event(id=event_id, type=event_type, data=data, occurred_at=occurred_at)
                await self._exchange.publish(to_message(event), routing_key=event.type)
            if rows:
                await conn.execute(
                    sql.SQL("UPDATE {}.outbox SET published_at = now() WHERE id = ANY(%s)").format(
                        self._schema
                    ),
                    ([row[0] for row in rows],),
                )
        return len(rows)

    async def run(self) -> None:
        while True:
            try:
                if await self.publish_pending() == 0:
                    await asyncio.sleep(self._idle_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("outbox relay failed; retrying")
                await asyncio.sleep(1)


def to_message(event: Event) -> Message:
    return Message(
        encode(event),
        message_id=str(event.id),
        type=event.type,
        content_type="application/json",
        delivery_mode=DeliveryMode.PERSISTENT,
    )
