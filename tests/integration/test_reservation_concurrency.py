import asyncio

from stockroom.inventory import service
from stockroom.inventory.domain import InsufficientStockError
from stockroom.shared.db import Pool
from tests.integration.conftest import CORE_WEST, EXP_WEST

TTL = 900


async def _available(pool: Pool) -> dict[str, tuple[int, int, int]]:
    return {
        level.key.sku: (level.on_hand, level.reserved, level.available)
        for level in await service.stock_levels(pool)
    }


async def test_200_concurrent_orders_for_50_units_reserve_exactly_50(pool: Pool) -> None:
    await service.receive(pool, CORE_WEST, 50, "test")

    results = await asyncio.gather(
        *(
            service.reserve(pool, f"order-{n}", [(CORE_WEST, 1)], ttl_seconds=TTL)
            for n in range(200)
        ),
        return_exceptions=True,
    )

    reserved = [r for r in results if isinstance(r, service.ReserveResult)]
    rejected = [r for r in results if isinstance(r, InsufficientStockError)]
    assert len(reserved) == 50
    assert len(rejected) == 150
    assert (await _available(pool))["CORE-001"] == (50, 50, 0)


async def test_multi_line_orders_in_opposite_order_do_not_deadlock(pool: Pool) -> None:
    await service.receive(pool, CORE_WEST, 30, "test")
    await service.receive(pool, EXP_WEST, 30, "test")

    results = await asyncio.gather(
        *(
            service.reserve(
                pool,
                f"order-{n}",
                [(CORE_WEST, 1), (EXP_WEST, 1)] if n % 2 else [(EXP_WEST, 1), (CORE_WEST, 1)],
                ttl_seconds=TTL,
            )
            for n in range(60)
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(r, service.ReserveResult) for r in results) == 30
    assert sum(isinstance(r, InsufficientStockError) for r in results) == 30
    levels = await _available(pool)
    assert levels["CORE-001"] == (30, 30, 0)
    assert levels["EXP-001"] == (30, 30, 0)


async def test_concurrent_retries_of_one_order_reserve_once(pool: Pool) -> None:
    await service.receive(pool, CORE_WEST, 10, "test")

    results = await asyncio.gather(
        *(service.reserve(pool, "order-1", [(CORE_WEST, 2)], ttl_seconds=TTL) for _ in range(10))
    )

    assert sum(r.created for r in results) == 1
    assert len({r.reservation.id for r in results}) == 1
    assert (await _available(pool))["CORE-001"] == (10, 2, 8)
