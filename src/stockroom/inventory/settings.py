from pydantic import Field

from stockroom.shared.settings import ServiceSettings


class InventorySettings(ServiceSettings):
    reservation_ttl_seconds: int = Field(default=900, gt=0)
