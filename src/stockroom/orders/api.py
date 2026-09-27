from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from stockroom.orders import handlers, service
from stockroom.orders.domain import (
    HistoryEntry,
    IdempotencyKeyReusedError,
    InvalidOrderTransitionError,
    Order,
    OrderLine,
    OrderNotFoundError,
    OrderStatus,
)
from stockroom.orders.settings import OrdersSettings
from stockroom.shared.db import Pool
from stockroom.shared.runtime import run_service


class LineItem(BaseModel):
    sku: str = Field(min_length=1)
    quantity: int = Field(gt=0)
    warehouse_id: str | None = None


class OrderWebhook(BaseModel):
    """The subset of a storefront order webhook this service needs. Unknown fields,
    including any customer data, are dropped on parse and never stored."""

    id: int | str
    line_items: list[LineItem] = Field(min_length=1)


class LineOut(BaseModel):
    sku: str
    warehouse_id: str
    quantity: int


class HistoryOut(BaseModel):
    status: str
    reason: str | None
    cause: str
    at: datetime


class OrderOut(BaseModel):
    id: UUID
    external_id: str
    status: str
    cancel_reason: str | None
    reservation_id: UUID | None
    tracking_number: str | None
    lines: list[LineOut]
    created_at: datetime
    history: list[HistoryOut] | None = None

    @classmethod
    def of(cls, order: Order, history: list[HistoryEntry] | None = None) -> "OrderOut":
        return cls(
            id=order.id,
            external_id=order.external_id,
            status=order.status.value,
            cancel_reason=order.cancel_reason,
            reservation_id=order.reservation_id,
            tracking_number=order.tracking_number,
            lines=[
                LineOut(sku=ln.sku, warehouse_id=ln.warehouse_id, quantity=ln.quantity)
                for ln in order.lines
            ],
            created_at=order.created_at,
            history=None
            if history is None
            else [
                HistoryOut(status=h.status.value, reason=h.reason, cause=h.cause, at=h.at)
                for h in history
            ],
        )


def get_pool(request: Request) -> Pool:
    pool: Pool = request.app.state.pool
    return pool


def get_settings(request: Request) -> OrdersSettings:
    settings: OrdersSettings = request.app.state.settings
    return settings


PoolDep = Annotated[Pool, Depends(get_pool)]
SettingsDep = Annotated[OrdersSettings, Depends(get_settings)]


def create_app(settings: OrdersSettings | None = None) -> FastAPI:
    settings = settings or OrdersSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with run_service(
            settings,
            schema=service.SCHEMA,
            routing_keys=handlers.ROUTING_KEYS,
            handlers=handlers.HANDLERS,
        ) as pool:
            app.state.pool = pool
            yield

    app = FastAPI(title="Stockroom orders", lifespan=lifespan)
    app.state.settings = settings
    _register_routes(app)
    _register_error_handlers(app)
    return app


def _register_routes(app: FastAPI) -> None:
    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks/orders", status_code=status.HTTP_201_CREATED)
    async def order_webhook(
        body: OrderWebhook,
        pool: PoolDep,
        settings: SettingsDep,
        response: Response,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ) -> OrderOut:
        result = await service.place_order(
            pool,
            idempotency_key=idempotency_key,
            payload=body.model_dump(mode="json"),
            external_id=str(body.id),
            lines=[
                OrderLine(
                    item.sku, item.warehouse_id or settings.default_warehouse_id, item.quantity
                )
                for item in body.line_items
            ],
        )
        response.status_code = result.status_code
        if result.replayed:
            response.headers["Idempotent-Replayed"] = "true"
        return OrderOut.of(result.order)

    @app.get("/orders")
    async def list_orders(
        pool: PoolDep,
        status_filter: Annotated[OrderStatus | None, Query(alias="status")] = None,
        limit: Annotated[int, Query(gt=0, le=500)] = 50,
    ) -> list[OrderOut]:
        return [
            OrderOut.of(order) for order in await service.list_recent(pool, status_filter, limit)
        ]

    @app.get("/orders/{order_id}")
    async def get_order(order_id: UUID, pool: PoolDep) -> OrderOut:
        order, history = await service.get(pool, order_id)
        return OrderOut.of(order, history)

    @app.post("/orders/{order_id}/cancel")
    async def cancel_order(order_id: UUID, pool: PoolDep) -> OrderOut:
        return OrderOut.of(await service.cancel(pool, order_id))


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(OrderNotFoundError)
    async def not_found(_: Request, exc: OrderNotFoundError) -> JSONResponse:
        return _error(status.HTTP_404_NOT_FOUND, "order_not_found")

    @app.exception_handler(InvalidOrderTransitionError)
    async def transition(_: Request, exc: InvalidOrderTransitionError) -> JSONResponse:
        return _error(status.HTTP_409_CONFLICT, "invalid_transition", status=exc.current.value)

    @app.exception_handler(IdempotencyKeyReusedError)
    async def key_reused(_: Request, exc: IdempotencyKeyReusedError) -> JSONResponse:
        return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "idempotency_key_reused")


def _error(status_code: int, error: str, **details: object) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": error, **details})
