"""Load a small fictional catalog. Safe to run more than once."""

import asyncio

from stockroom.inventory.settings import InventorySettings
from stockroom.shared.db import Connection, create_pool
from stockroom.shared.migrations import apply_migrations

WAREHOUSES = [("eu-west", "Western Europe DC"), ("eu-central", "Central Europe DC")]

SKUS = [
    ("CORE-001", "Core Game Box"),
    ("EXP-001", "Expansion Pack I: Coastlines"),
    ("EXP-002", "Expansion Pack II: Highlands"),
    ("SLV-100", "Card Sleeves (100)"),
]

OPENING_STOCK = {
    ("CORE-001", "eu-west"): 50,
    ("CORE-001", "eu-central"): 30,
    ("EXP-001", "eu-west"): 40,
    ("EXP-002", "eu-west"): 25,
    ("EXP-002", "eu-central"): 10,
    ("SLV-100", "eu-west"): 200,
}

SEED_REFERENCE = "opening-stock"


async def seed(conn: Connection) -> None:
    async with conn.transaction():
        async with conn.cursor() as cursor:
            await cursor.executemany(
                "INSERT INTO inventory.warehouses (id, name) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                WAREHOUSES,
            )
            await cursor.executemany(
                "INSERT INTO inventory.skus (sku, name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                SKUS,
            )
            await cursor.executemany(
                "INSERT INTO inventory.stock_items (sku, warehouse_id) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                list(OPENING_STOCK),
            )
        cursor = await conn.execute(
            "SELECT 1 FROM inventory.stock_movements WHERE reference = %s LIMIT 1",
            (SEED_REFERENCE,),
        )
        if await cursor.fetchone() is not None:
            return
        async with conn.cursor() as cursor:
            await cursor.executemany(
                "INSERT INTO inventory.stock_movements "
                "(sku, warehouse_id, quantity, reason, reference) "
                "VALUES (%s, %s, %s, 'receipt', %s)",
                [(sku, wh, qty, SEED_REFERENCE) for (sku, wh), qty in OPENING_STOCK.items()],
            )


async def main() -> None:
    settings = InventorySettings()
    async with create_pool(settings.database_url, max_size=1) as pool, pool.connection() as conn:
        await apply_migrations(conn, service="inventory", package="stockroom.inventory.migrations")
        await seed(conn)


if __name__ == "__main__":
    asyncio.run(main())
