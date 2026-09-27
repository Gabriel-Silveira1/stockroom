from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from stockroom.inventory import handlers, service
from stockroom.inventory.domain import (
    IdempotencyConflictError,
    InsufficientStockError,
    InvalidReservationRequestError,
    InvalidTransitionError,
    Reservation,
    ReservationNotFoundError,
    StockKey,
    UnknownStockItemsError,
)
from stockroom.inventory.settings import InventorySettings
from stockroom.shared.db import Pool
from stockroom.shared.runtime import run_service


class Line(BaseModel):
    sku: str = Field(min_length=1)
    warehouse_id: str = Field(min_length=1)
    quantity: int = Field(gt=0)


class ReservationIn(BaseModel):
    order_id: str = Field(min_length=1, max_length=100)
    lines: list[Line] = Field(min_length=1)


class ReservationOut(BaseModel):
    id: UUID
    order_id: str
    status: str
    expires_at: datetime
    lines: list[Line]

    @classmethod
    def of(cls, reservation: Reservation) -> "ReservationOut":
        return cls(
            id=reservation.id,
            order_id=reservation.order_id,
            status=reservation.status.value,
            expires_at=reservation.expires_at,
            lines=[
                Line(sku=key.sku, warehouse_id=key.warehouse_id, quantity=quantity)
                for key, quantity in reservation.lines.items()
            ],
        )


class ReceiptIn(Line):
    reference: str | None = None


class StockLevelOut(BaseModel):
    sku: str
    warehouse_id: str
    on_hand: int
    reserved: int
    available: int


def get_pool(request: Request) -> Pool:
    pool: Pool = request.app.state.pool
    return pool


def get_settings(request: Request) -> InventorySettings:
    settings: InventorySettings = request.app.state.settings
    return settings


PoolDep = Annotated[Pool, Depends(get_pool)]
SettingsDep = Annotated[InventorySettings, Depends(get_settings)]


def create_app(settings: InventorySettings | None = None) -> FastAPI:
    settings = settings or InventorySettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with run_service(
            settings,
            schema=handlers.SCHEMA,
            routing_keys=handlers.ROUTING_KEYS,
            handlers=handlers.build_handlers(ttl_seconds=settings.reservation_ttl_seconds),
        ) as pool:
            app.state.pool = pool
            yield

    app = FastAPI(title="Stockroom inventory", lifespan=lifespan)
    app.state.settings = settings
    _register_routes(app)
    _register_error_handlers(app)
    return app


def _register_routes(app: FastAPI) -> None:
    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/stock")
    async def list_stock(pool: PoolDep, warehouse_id: str | None = None) -> list[StockLevelOut]:
        levels = await service.stock_levels(pool, warehouse_id)
        return [
            StockLevelOut(
                sku=level.key.sku,
                warehouse_id=level.key.warehouse_id,
                on_hand=level.on_hand,
                reserved=level.reserved,
                available=level.available,
            )
            for level in levels
        ]

    @app.post("/receipts", status_code=status.HTTP_201_CREATED)
    async def receive(body: ReceiptIn, pool: PoolDep) -> None:
        key = StockKey(body.sku, body.warehouse_id)
        await service.receive(pool, key, body.quantity, body.reference)

    @app.post("/reservations", status_code=status.HTTP_201_CREATED)
    async def reserve(
        body: ReservationIn, pool: PoolDep, settings: SettingsDep, response: Response
    ) -> ReservationOut:
        result = await service.reserve(
            pool,
            body.order_id,
            ((StockKey(line.sku, line.warehouse_id), line.quantity) for line in body.lines),
            ttl_seconds=settings.reservation_ttl_seconds,
        )
        if not result.created:
            response.status_code = status.HTTP_200_OK
        return ReservationOut.of(result.reservation)

    @app.get("/reservations/{reservation_id}")
    async def get_reservation(reservation_id: UUID, pool: PoolDep) -> ReservationOut:
        return ReservationOut.of(await service.get(pool, reservation_id))

    @app.post("/reservations/{reservation_id}/commit")
    async def commit(reservation_id: UUID, pool: PoolDep) -> ReservationOut:
        return ReservationOut.of(await service.commit(pool, reservation_id))

    @app.post("/reservations/{reservation_id}/release")
    async def release(reservation_id: UUID, pool: PoolDep) -> ReservationOut:
        return ReservationOut.of(await service.release(pool, reservation_id))


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(InsufficientStockError)
    async def insufficient(_: Request, exc: InsufficientStockError) -> JSONResponse:
        shortfalls = [
            {
                "sku": s.key.sku,
                "warehouse_id": s.key.warehouse_id,
                "requested": s.requested,
                "available": s.available,
            }
            for s in exc.shortfalls
        ]
        return _error(status.HTTP_409_CONFLICT, "insufficient_stock", shortfalls=shortfalls)

    @app.exception_handler(UnknownStockItemsError)
    async def unknown(_: Request, exc: UnknownStockItemsError) -> JSONResponse:
        items = [{"sku": k.sku, "warehouse_id": k.warehouse_id} for k in exc.keys]
        return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "unknown_stock_items", items=items)

    @app.exception_handler(InvalidReservationRequestError)
    async def invalid(_: Request, exc: InvalidReservationRequestError) -> JSONResponse:
        return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_request", message=str(exc))

    @app.exception_handler(ReservationNotFoundError)
    async def not_found(_: Request, exc: ReservationNotFoundError) -> JSONResponse:
        return _error(status.HTTP_404_NOT_FOUND, "reservation_not_found")

    @app.exception_handler(IdempotencyConflictError)
    async def idempotency(_: Request, exc: IdempotencyConflictError) -> JSONResponse:
        return _error(status.HTTP_409_CONFLICT, "idempotency_conflict", order_id=exc.order_id)

    @app.exception_handler(InvalidTransitionError)
    async def transition(_: Request, exc: InvalidTransitionError) -> JSONResponse:
        return _error(status.HTTP_409_CONFLICT, "invalid_transition", status=exc.current.value)


def _error(status_code: int, error: str, **details: object) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": error, **details})
