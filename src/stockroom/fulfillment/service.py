import asyncio
import logging
from collections.abc import Sequence
from datetime import timedelta
from uuid import UUID, uuid4

from stockroom.fulfillment import repository
from stockroom.fulfillment.carrier import Carrier, CarrierError
from stockroom.fulfillment.domain import (
    DispatchOutcome,
    Shipment,
    ShipmentLine,
    ShipmentStatus,
    after_failure,
    can_cancel,
    retry_delay,
)
from stockroom.shared.db import Connection, Pool
from stockroom.shared.events import Event
from stockroom.shared.outbox import enqueue

SCHEMA = "fulfillment"

log = logging.getLogger(__name__)


async def start_shipment_in(
    conn: Connection, order_id: UUID, reservation_id: UUID, lines: Sequence[ShipmentLine]
) -> bool:
    """Pick and pack the order. False when a shipment (or a tombstone) already exists."""
    shipment_id = uuid4()
    if not await repository.try_insert_shipment(conn, shipment_id, order_id, reservation_id, lines):
        return False
    await _emit(
        conn,
        "fulfillment.picked",
        order_id,
        reservation_id,
        shipment_id=str(shipment_id),
    )
    return True


async def cancel_in(conn: Connection, order_id: UUID) -> bool:
    """Stop a shipment that has not left. Before it exists, leave a tombstone so a late
    `order.reserved` cannot start one."""
    shipment = await repository.get_by_order(conn, order_id, lock=True)
    if shipment is None:
        await repository.insert_tombstone(conn, uuid4(), order_id)
        return True
    if not can_cancel(shipment.status):
        return False
    await repository.mark_final(conn, shipment.id, ShipmentStatus.CANCELLED, None)
    return True


class Dispatcher:
    """Hands picked shipments to the carrier, retrying with backoff, giving up at last."""

    def __init__(
        self,
        pool: Pool,
        carrier: Carrier,
        *,
        max_attempts: int,
        base_delay: timedelta,
        max_delay: timedelta,
        idle_seconds: float = 0.5,
    ) -> None:
        self._pool = pool
        self._carrier = carrier
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._idle_seconds = idle_seconds

    async def dispatch_next(self) -> bool:
        """Dispatch one due shipment. False when none was due."""
        async with self._pool.connection() as conn, conn.transaction():
            shipment = await repository.lock_next_due(conn)
            if shipment is None:
                return False
            try:
                tracking = await self._carrier.book(shipment.id, shipment.order_id, shipment.lines)
            except CarrierError as exc:
                await self._record_failure(conn, shipment, exc)
            else:
                await repository.mark_shipped(conn, shipment.id, tracking)
                await _emit(
                    conn,
                    "fulfillment.shipped",
                    shipment.order_id,
                    shipment.reservation_id,
                    tracking_number=tracking,
                )
        return True

    async def run(self) -> None:
        while True:
            try:
                if not await self.dispatch_next():
                    await asyncio.sleep(self._idle_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("dispatcher failed; retrying")
                await asyncio.sleep(1)

    async def _record_failure(
        self, conn: Connection, shipment: Shipment, error: CarrierError
    ) -> None:
        outcome = after_failure(
            shipment.attempts, max_attempts=self._max_attempts, permanent=error.permanent
        )
        if outcome is DispatchOutcome.RETRY:
            delay = retry_delay(shipment.attempts + 1, base=self._base_delay, cap=self._max_delay)
            log.warning("shipment %s: %s; retrying in %s", shipment.id, error, delay)
            await repository.schedule_retry(conn, shipment.id, str(error), delay)
            return
        log.error("shipment %s failed for good: %s", shipment.id, error)
        await repository.mark_final(conn, shipment.id, ShipmentStatus.FAILED, str(error))
        await _emit(
            conn,
            "fulfillment.failed",
            shipment.order_id,
            shipment.reservation_id,
            reason="carrier_failed",
            detail=str(error),
        )


async def get(pool: Pool, order_id: UUID) -> Shipment | None:
    async with pool.connection() as conn:
        return await repository.get_by_order(conn, order_id)


async def list_recent(pool: Pool, status: ShipmentStatus | None, limit: int) -> list[Shipment]:
    async with pool.connection() as conn:
        return await repository.list_shipments(conn, status, limit)


async def _emit(
    conn: Connection,
    event_type: str,
    order_id: UUID,
    reservation_id: UUID | None,
    **data: object,
) -> None:
    payload = {"order_id": str(order_id), "reservation_id": str(reservation_id), **data}
    await enqueue(conn, SCHEMA, Event(event_type, payload))
