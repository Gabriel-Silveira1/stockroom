"""An in-memory stand-in for the broker: drain each outbox and hand events to handlers.

It exercises the real handlers, transactions and deduplication against Postgres, without
the timing of a live RabbitMQ. `test_messaging_e2e` covers the broker itself.
"""

from collections.abc import Mapping
from fnmatch import fnmatch

from psycopg import sql

from stockroom.inventory import handlers as inventory_handlers
from stockroom.orders import handlers as orders_handlers
from stockroom.shared.consumer import Handler, process
from stockroom.shared.db import Pool
from stockroom.shared.events import Event

TTL = 900

SUBSCRIBERS: dict[str, tuple[tuple[str, ...], Mapping[str, Handler]]] = {
    "inventory": (
        inventory_handlers.ROUTING_KEYS,
        inventory_handlers.build_handlers(ttl_seconds=TTL),
    ),
    "orders": (orders_handlers.ROUTING_KEYS, orders_handlers.HANDLERS),
}


async def drain(pool: Pool, schema: str) -> list[Event]:
    async with pool.connection() as conn, conn.transaction():
        cursor = await conn.execute(
            sql.SQL(
                "UPDATE {}.outbox SET published_at = now() WHERE published_at IS NULL "
                "RETURNING position, id, type, data, occurred_at"
            ).format(sql.Identifier(schema))
        )
        rows = sorted(await cursor.fetchall())
    return [Event(id=i, type=t, data=d, occurred_at=at) for _, i, t, d, at in rows]


async def deliver(pool: Pool, event: Event) -> None:
    for schema, (routing_keys, handlers) in SUBSCRIBERS.items():
        if any(fnmatch(event.type, key) for key in routing_keys):
            await process(pool, schema, handlers, event)


async def pump(pool: Pool) -> list[Event]:
    """Deliver events until every outbox is empty. Returns everything that was sent."""
    sent: list[Event] = []
    while True:
        batch = [event for schema in SUBSCRIBERS for event in await drain(pool, schema)]
        if not batch:
            return sent
        for event in batch:
            await deliver(pool, event)
        sent.extend(batch)
