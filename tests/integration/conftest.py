import asyncio
import os
import sys
from collections.abc import AsyncIterator, Callable

import psycopg
import pytest

from stockroom.inventory.domain import StockKey
from stockroom.shared.db import Pool, create_pool
from stockroom.shared.migrations import apply_migrations

DATABASE_URL = os.environ.get(
    "STOCKROOM_TEST_DATABASE_URL",
    "postgresql://stockroom:stockroom@localhost:5433/stockroom_test",
)

SCHEMAS = ("inventory", "orders")

CORE_WEST = StockKey("CORE-001", "eu-west")
EXP_WEST = StockKey("EXP-001", "eu-west")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "integration" in item.path.parts:
            item.add_marker(pytest.mark.integration)


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> dict[str, Callable[[], asyncio.AbstractEventLoop]]:
    # psycopg's async mode cannot run on the Proactor loop that Windows uses by default.
    if sys.platform == "win32":
        return {"selector": asyncio.SelectorEventLoop}
    return {"default": asyncio.new_event_loop}


@pytest.fixture(scope="session")
async def pool() -> AsyncIterator[Pool]:
    async with await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True) as conn:
        for schema in SCHEMAS:
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.execute("DROP TABLE IF EXISTS public.schema_migrations")
    async with create_pool(DATABASE_URL, max_size=20) as pool:
        async with pool.connection() as conn:
            for schema in SCHEMAS:
                await apply_migrations(
                    conn, service=schema, package=f"stockroom.{schema}.migrations"
                )
        yield pool


@pytest.fixture(autouse=True)
async def catalog(pool: Pool) -> None:
    """Every test starts from empty services and a ledger with two known stock items."""
    async with pool.connection() as conn:
        await conn.execute(
            "TRUNCATE inventory.reservation_lines, inventory.reservations, "
            "inventory.stock_movements, inventory.stock_items, inventory.skus, "
            "inventory.warehouses, inventory.outbox, inventory.processed_messages, "
            "orders.idempotency_keys, orders.order_history, orders.order_lines, "
            "orders.orders, orders.outbox, orders.processed_messages"
        )
        await conn.execute("INSERT INTO inventory.warehouses VALUES ('eu-west', 'West')")
        await conn.execute(
            "INSERT INTO inventory.skus VALUES ('CORE-001', 'Core'), ('EXP-001', 'Expansion')"
        )
        await conn.execute(
            "INSERT INTO inventory.stock_items VALUES ('CORE-001', 'eu-west'), "
            "('EXP-001', 'eu-west')"
        )
