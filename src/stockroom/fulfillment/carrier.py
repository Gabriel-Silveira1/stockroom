from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

import httpx

from stockroom.fulfillment.domain import ShipmentLine


class CarrierError(Exception):
    def __init__(self, message: str, *, permanent: bool) -> None:
        self.permanent = permanent
        super().__init__(message)


class Carrier(Protocol):
    async def book(self, shipment_id: UUID, order_id: UUID, lines: Sequence[ShipmentLine]) -> str:
        """Book the shipment and return its tracking number.

        Must be idempotent per shipment id: the dispatcher retries after timeouts and
        crashes, and a retry must not book a second truck.
        """
        ...


class HttpCarrier:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def book(self, shipment_id: UUID, order_id: UUID, lines: Sequence[ShipmentLine]) -> str:
        try:
            response = await self._client.post(
                "/shipments",
                headers={"Idempotency-Key": str(shipment_id)},
                json={
                    "order_id": str(order_id),
                    "parcels": [{"sku": line.sku, "quantity": line.quantity} for line in lines],
                },
            )
        except httpx.TransportError as exc:
            raise CarrierError(f"carrier unreachable: {exc!r}", permanent=False) from exc
        if response.status_code >= 500 or response.status_code == 429:
            raise CarrierError(f"carrier answered {response.status_code}", permanent=False)
        if response.is_error:
            raise CarrierError(f"carrier rejected: {response.status_code}", permanent=True)
        tracking: str = response.json()["tracking_number"]
        return tracking
