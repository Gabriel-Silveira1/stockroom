import logging
from uuid import UUID

from stockroom.orders import repository, service
from stockroom.orders.domain import InvalidOrderTransitionError, OrderStatus
from stockroom.orders.service import SCHEMA
from stockroom.shared.consumer import Handler
from stockroom.shared.db import Connection
from stockroom.shared.events import Event
from stockroom.shared.outbox import enqueue

ROUTING_KEYS = (
    "stock.reserved",
    "stock.rejected",
    "fulfillment.picked",
    "fulfillment.shipped",
    "fulfillment.failed",
)

log = logging.getLogger(__name__)


async def _advance(
    conn: Connection,
    event: Event,
    target: OrderStatus,
    *,
    reservation_id: UUID | None = None,
    tracking_number: str | None = None,
) -> bool:
    """Apply the move an event implies. Late events for a cancelled order re-announce the
    cancellation, because whoever sent them is now holding something for it."""
    order_id = UUID(event.data["order_id"])
    try:
        return await service.transition_in(
            conn,
            order_id,
            target,
            cause=event.type,
            reservation_id=reservation_id,
            tracking_number=tracking_number,
        )
    except InvalidOrderTransitionError as exc:
        if exc.current is OrderStatus.CANCELLED and target is not OrderStatus.SHIPPED:
            late_reservation = event.data.get("reservation_id")
            await service.announce_cancellation(
                conn,
                order_id,
                "cancelled_while_in_progress",
                UUID(late_reservation) if late_reservation else None,
            )
        elif exc.current is OrderStatus.CANCELLED:
            log.error("order %s shipped after it was cancelled", order_id)
        else:
            log.info("ignoring late %s for order %s in %s", event.type, order_id, exc.current)
        return False


async def on_stock_reserved(conn: Connection, event: Event) -> None:
    order_id = UUID(event.data["order_id"])
    reservation_id = UUID(event.data["reservation_id"])
    applied = await _advance(conn, event, OrderStatus.RESERVED, reservation_id=reservation_id)
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


async def on_rejected(conn: Connection, event: Event) -> None:
    """Inventory could not hold the stock, or the carrier gave up: cancel the order."""
    order_id = UUID(event.data["order_id"])
    try:
        await service.cancel_in(conn, order_id, cause=event.type, reason=event.data["reason"])
    except InvalidOrderTransitionError as exc:
        log.info("ignoring %s for order %s in %s", event.type, order_id, exc.current)


async def on_picked(conn: Connection, event: Event) -> None:
    await _advance(conn, event, OrderStatus.PICKED)


async def on_shipped(conn: Connection, event: Event) -> None:
    await _advance(conn, event, OrderStatus.SHIPPED, tracking_number=event.data["tracking_number"])


HANDLERS: dict[str, Handler] = {
    "stock.reserved": on_stock_reserved,
    "stock.rejected": on_rejected,
    "fulfillment.picked": on_picked,
    "fulfillment.shipped": on_shipped,
    "fulfillment.failed": on_rejected,
}
