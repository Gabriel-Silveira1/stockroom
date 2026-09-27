"""A stand-in shipping carrier that fails on purpose.

It answers 503 for a configurable share of requests and can be made slower, so the
dispatcher's retries, backoff and compensation can be seen working. Bookings are
idempotent by the `Idempotency-Key` header, like a real carrier API: a retried request
gets the tracking number it was already given. State is in memory; it is a test double.
"""

import asyncio
import random
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, Header, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class CarrierSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STOCKROOM_CARRIER_")

    failure_rate: float = Field(default=0.3, ge=0, le=1)
    latency_ms: int = Field(default=50, ge=0)


class Parcel(BaseModel):
    sku: str
    quantity: int = Field(gt=0)


class BookingIn(BaseModel):
    order_id: UUID
    parcels: list[Parcel] = Field(min_length=1)


class BookingOut(BaseModel):
    tracking_number: str


class Behaviour(BaseModel):
    failure_rate: float = Field(ge=0, le=1)
    latency_ms: int = Field(ge=0)


def create_app(
    settings: CarrierSettings | None = None, rng: random.Random | None = None
) -> FastAPI:
    settings = settings or CarrierSettings()
    rng = rng or random.Random()
    behaviour = Behaviour(failure_rate=settings.failure_rate, latency_ms=settings.latency_ms)
    bookings: dict[str, str] = {}
    app = FastAPI(title="Stockroom mock carrier")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/shipments", status_code=status.HTTP_201_CREATED, response_model=None)
    async def book(
        body: BookingIn, idempotency_key: Annotated[str, Header(min_length=1)]
    ) -> BookingOut | JSONResponse:
        await asyncio.sleep(behaviour.latency_ms / 1000)
        if idempotency_key in bookings:
            return BookingOut(tracking_number=bookings[idempotency_key])
        if rng.random() < behaviour.failure_rate:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"error": "carrier_unavailable"},
            )
        tracking = f"TRK{rng.randrange(10**9):09d}"
        bookings[idempotency_key] = tracking
        return BookingOut(tracking_number=tracking)

    @app.get("/admin/behaviour")
    async def get_behaviour() -> Behaviour:
        return behaviour

    @app.put("/admin/behaviour")
    async def set_behaviour(body: Behaviour) -> Behaviour:
        behaviour.failure_rate = body.failure_rate
        behaviour.latency_ms = body.latency_ms
        return behaviour

    return app
