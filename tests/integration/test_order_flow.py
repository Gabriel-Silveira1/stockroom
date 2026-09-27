from uuid import UUID

from stockroom.inventory import service as inventory
from stockroom.orders import service as orders
from stockroom.orders.domain import OrderLine, OrderStatus
from stockroom.shared.consumer import process
from stockroom.shared.db import Pool
from tests.integration.bus import SUBSCRIBERS, drain, pump
from tests.integration.conftest import CORE_WEST


async def _place(pool: Pool, external_id: str, quantity: int) -> UUID:
    result = await orders.place_order(
        pool,
        idempotency_key=f"key-{external_id}",
        payload={"id": external_id, "quantity": quantity},
        external_id=external_id,
        lines=[OrderLine("CORE-001", "eu-west", quantity)],
    )
    return result.order.id


async def _stock(pool: Pool) -> tuple[int, int, int]:
    [level] = [lvl for lvl in await inventory.stock_levels(pool) if lvl.key == CORE_WEST]
    return level.on_hand, level.reserved, level.available


async def test_placed_order_is_reserved_then_picked(pool: Pool) -> None:
    await inventory.receive(pool, CORE_WEST, 5, "test")
    order_id = await _place(pool, "1001", 2)

    sent = await pump(pool)

    order, history = await orders.get(pool, order_id)
    assert [e.type for e in sent] == [
        "order.placed",
        "stock.reserved",
        "order.reserved",
        "fulfillment.picked",
    ]
    assert order.status is OrderStatus.PICKED
    assert order.reservation_id is not None
    assert [h.status for h in history] == [
        OrderStatus.PENDING,
        OrderStatus.RESERVED,
        OrderStatus.PICKED,
    ]
    assert await _stock(pool) == (5, 2, 3)


async def test_order_without_stock_is_cancelled(pool: Pool) -> None:
    await inventory.receive(pool, CORE_WEST, 1, "test")
    order_id = await _place(pool, "1001", 2)

    sent = await pump(pool)

    order, _ = await orders.get(pool, order_id)
    assert [e.type for e in sent] == ["order.placed", "stock.rejected", "order.cancelled"]
    assert order.status is OrderStatus.CANCELLED
    assert order.cancel_reason == "out_of_stock"
    assert await _stock(pool) == (1, 0, 1)


async def test_cancelling_a_reserved_order_releases_its_stock(pool: Pool) -> None:
    await inventory.receive(pool, CORE_WEST, 5, "test")
    order_id = await _place(pool, "1001", 2)
    await pump(pool)

    await orders.cancel(pool, order_id)
    sent = await pump(pool)

    assert [e.type for e in sent] == ["order.cancelled", "stock.released"]
    assert await _stock(pool) == (5, 0, 5)


async def test_order_cancelled_before_reservation_still_frees_the_late_hold(pool: Pool) -> None:
    await inventory.receive(pool, CORE_WEST, 5, "test")
    order_id = await _place(pool, "1001", 2)
    placed = await drain(pool, "orders")

    await orders.cancel(pool, order_id)
    for event in placed:
        await process(pool, "inventory", SUBSCRIBERS["inventory"][1], event)
    await pump(pool)

    order, _ = await orders.get(pool, order_id)
    assert order.status is OrderStatus.CANCELLED
    assert await _stock(pool) == (5, 0, 5)


async def test_redelivered_event_is_handled_once(pool: Pool) -> None:
    await inventory.receive(pool, CORE_WEST, 5, "test")
    await _place(pool, "1001", 2)
    [placed] = await drain(pool, "orders")
    handlers = SUBSCRIBERS["inventory"][1]

    first = await process(pool, "inventory", handlers, placed)
    second = await process(pool, "inventory", handlers, placed)

    assert (first, second) == (True, False)
    assert len(await drain(pool, "inventory")) == 1
