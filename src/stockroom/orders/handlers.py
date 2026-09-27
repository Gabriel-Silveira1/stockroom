import logging
from uuid import UUID

from stockroom.orders import repository, service
from stockroom.orders.domain import InvalidOrderTransitionError, OrderStatus
from stockroom.orders.service import SCHEMA
from stockroom.shared.consumer import Handler
from stockroom.shared.db import Connection
from stockroom.shared.events import Event
from stockroom.shared.outbox import enqueue

ROUTING_KEYS = ("stock.reserved", "stock.rejected")

log = logging.getLogger(__name__)


async def on_stock_reserved(conn: Connection, event: Event) -> None:
    order_id = UUID(event.data["order_id"])
    reservation_id = UUID(event.data["reservation_id"])
    try:
        applied = await service.transition_in(
            conn, order_id, OrderStatus.RESERVED, cause=event.type, reservation_id=reservation_id
        )
    except InvalidOrderTransitionError as exc:
        if exc.current is OrderStatus.CANCELLED:
            # The order was cancelled while inventory was still reserving. Say so again:
            # inventory now holds stock for it and must let go.
            await service.announce_cancellation(conn, order_id, "cancelled_before_reservation")
        else:
            log.info("ignoring late %s for order %s in %s", event.type, order_id, exc.current)
        return
    if not applied:
        return
    order = await repository.get_order(conn, order_id)
    assert order is not None
    await enqueue(
        conn,
        SCHEMA,
        Event(
            "order.reserved",
            {
                "order_id": str(order_id),
                "reservation_id": str(reservation_id),
                "lines": [
                    {"sku": ln.sku, "warehouse_id": ln.warehouse_id, "quantity": ln.quantity}
                    for ln in order.lines
                ],
            },
        ),
    )


async def on_stock_rejected(conn: Connection, event: Event) -> None:
    order_id = UUID(event.data["order_id"])
    try:
        await service.cancel_in(conn, order_id, cause=event.type, reason=event.data["reason"])
    except InvalidOrderTransitionError as exc:
        log.info("ignoring %s for order %s in %s", event.type, order_id, exc.current)


HANDLERS: dict[str, Handler] = {
    "stock.reserved": on_stock_reserved,
    "stock.rejected": on_stock_rejected,
}
