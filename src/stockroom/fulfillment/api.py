import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from stockroom.fulfillment import handlers, service
from stockroom.fulfillment.carrier import HttpCarrier
from stockroom.fulfillment.domain import Shipment, ShipmentStatus
from stockroom.fulfillment.settings import FulfillmentSettings
from stockroom.shared.db import Pool
from stockroom.shared.runtime import run_service


class LineOut(BaseModel):
    sku: str
    warehouse_id: str
    quantity: int


class ShipmentOut(BaseModel):
    id: UUID
    order_id: UUID
    status: str
    attempts: int
    next_attempt_at: datetime
    last_error: str | None
    tracking_number: str | None
    lines: list[LineOut]

    @classmethod
    def of(cls, shipment: Shipment) -> "ShipmentOut":
        return cls(
            id=shipment.id,
            order_id=shipment.order_id,
            status=shipment.status.value,
            attempts=shipment.attempts,
            next_attempt_at=shipment.next_attempt_at,
            last_error=shipment.last_error,
            tracking_number=shipment.tracking_number,
            lines=[
                LineOut(sku=ln.sku, warehouse_id=ln.warehouse_id, quantity=ln.quantity)
                for ln in shipment.lines
            ],
        )


def get_pool(request: Request) -> Pool:
    pool: Pool = request.app.state.pool
    return pool


PoolDep = Annotated[Pool, Depends(get_pool)]


def create_app(settings: FulfillmentSettings | None = None) -> FastAPI:
    settings = settings or FulfillmentSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with (
            run_service(
                settings,
                schema=service.SCHEMA,
                routing_keys=handlers.ROUTING_KEYS,
                handlers=handlers.HANDLERS,
            ) as pool,
            httpx.AsyncClient(
                base_url=settings.carrier_url, timeout=settings.carrier_timeout_seconds
            ) as client,
        ):
            dispatcher = service.Dispatcher(
                pool,
                HttpCarrier(client),
                max_attempts=settings.dispatch_max_attempts,
                base_delay=timedelta(seconds=settings.dispatch_base_delay_seconds),
                max_delay=timedelta(seconds=settings.dispatch_max_delay_seconds),
            )
            workers = [
                asyncio.create_task(dispatcher.run(), name=f"dispatcher-{n}")
                for n in range(settings.dispatch_workers)
            ]
            app.state.pool = pool
            try:
                yield
            finally:
                for worker in workers:
                    worker.cancel()
                for worker in workers:
                    with suppress(asyncio.CancelledError):
                        await worker

    app = FastAPI(title="Stockroom fulfillment", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/shipments")
    async def list_shipments(
        pool: PoolDep,
        status_filter: Annotated[ShipmentStatus | None, Query(alias="status")] = None,
        limit: Annotated[int, Query(gt=0, le=500)] = 50,
    ) -> list[ShipmentOut]:
        shipments = await service.list_recent(pool, status_filter, limit)
        return [ShipmentOut.of(shipment) for shipment in shipments]

    @app.get("/shipments/{order_id}", response_model=None)
    async def get_shipment(order_id: UUID, pool: PoolDep) -> ShipmentOut | JSONResponse:
        shipment = await service.get(pool, order_id)
        if shipment is None:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND, content={"error": "shipment_not_found"}
            )
        return ShipmentOut.of(shipment)

    return app
