from uuid import UUID

from stockroom.fulfillment import service
from stockroom.fulfillment.domain import ShipmentLine
from stockroom.shared.consumer import Handler
from stockroom.shared.db import Connection
from stockroom.shared.events import Event

ROUTING_KEYS = ("order.reserved", "order.cancelled")


async def on_order_reserved(conn: Connection, event: Event) -> None:
    lines = [
        ShipmentLine(line["sku"], line["warehouse_id"], int(line["quantity"]))
        for line in event.data["lines"]
    ]
    await service.start_shipment_in(
        conn, UUID(event.data["order_id"]), UUID(event.data["reservation_id"]), lines
    )


async def on_order_cancelled(conn: Connection, event: Event) -> None:
    await service.cancel_in(conn, UUID(event.data["order_id"]))


HANDLERS: dict[str, Handler] = {
    "order.reserved": on_order_reserved,
    "order.cancelled": on_order_cancelled,
}
