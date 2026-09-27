import logging
from uuid import UUID

from stockroom.inventory import repository, service
from stockroom.inventory.domain import (
    InsufficientStockError,
    InvalidReservationRequestError,
    InvalidTransitionError,
    ReservationStatus,
    StockKey,
    UnknownStockItemsError,
)
from stockroom.shared.consumer import Handler
from stockroom.shared.db import Connection
from stockroom.shared.events import Event, EventData
from stockroom.shared.outbox import enqueue

SCHEMA = "inventory"
QUEUE = "inventory"
ROUTING_KEYS = ("order.placed", "order.cancelled", "fulfillment.picked", "fulfillment.shipped")

log = logging.getLogger(__name__)


def build_handlers(*, ttl_seconds: int) -> dict[str, Handler]:
    async def on_order_placed(conn: Connection, event: Event) -> None:
        order_id = str(event.data["order_id"])
        lines = [
            (StockKey(line["sku"], line["warehouse_id"]), int(line["quantity"]))
            for line in event.data["lines"]
        ]
        try:
            # A savepoint: a rejected reservation must roll back on its own, without
            # undoing the processed-message record or the rejection event below.
            async with conn.transaction():
                result = await service.reserve_in(conn, order_id, lines, ttl_seconds=ttl_seconds)
        except InsufficientStockError as exc:
            shortfalls = [
                {
                    "sku": s.key.sku,
                    "warehouse_id": s.key.warehouse_id,
                    "requested": s.requested,
                    "available": s.available,
                }
                for s in exc.shortfalls
            ]
            await _emit(
                conn, "stock.rejected", order_id, reason="out_of_stock", shortfalls=shortfalls
            )
            return
        except (UnknownStockItemsError, InvalidReservationRequestError) as exc:
            await _emit(conn, "stock.rejected", order_id, reason="invalid_items", detail=str(exc))
            return
        reservation = result.reservation
        await _emit(
            conn,
            "stock.reserved",
            order_id,
            reservation_id=str(reservation.id),
            expires_at=reservation.expires_at.isoformat(),
        )

    async def on_order_cancelled(conn: Connection, event: Event) -> None:
        order_id = str(event.data["order_id"])
        reservation = await repository.get_reservation_by_order(conn, order_id)
        if reservation is None or reservation.status is not ReservationStatus.ACTIVE:
            return
        try:
            await service.release_in(conn, reservation.id)
        except InvalidTransitionError:
            log.warning("order %s cancelled after its stock shipped; nothing to release", order_id)
            return
        await _emit(conn, "stock.released", order_id, reservation_id=str(reservation.id))

    async def on_picked(conn: Connection, event: Event) -> None:
        order_id = str(event.data["order_id"])
        try:
            await service.pin_in(conn, UUID(event.data["reservation_id"]))
        except InvalidTransitionError as exc:
            # Picked too late: the hold lapsed and its units may be promised elsewhere.
            await _emit(conn, "stock.rejected", order_id, reason=f"reservation_{exc.current}")

    async def on_shipped(conn: Connection, event: Event) -> None:
        try:
            await service.commit_in(conn, UUID(event.data["reservation_id"]))
        except InvalidTransitionError as exc:
            log.error(
                "order %s shipped but its reservation is %s; the ledger needs a manual correction",
                event.data["order_id"],
                exc.current,
            )

    return {
        "order.placed": on_order_placed,
        "order.cancelled": on_order_cancelled,
        "fulfillment.picked": on_picked,
        "fulfillment.shipped": on_shipped,
    }


async def _emit(conn: Connection, event_type: str, order_id: str, **data: object) -> None:
    payload: EventData = {"order_id": order_id, **data}
    await enqueue(conn, SCHEMA, Event(event_type, payload))
