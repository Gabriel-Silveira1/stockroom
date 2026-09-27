from pydantic import Field

from stockroom.shared.settings import ServiceSettings


class FulfillmentSettings(ServiceSettings):
    carrier_url: str = "http://localhost:8104"
    carrier_timeout_seconds: float = Field(default=2.0, gt=0)
    dispatch_workers: int = Field(default=4, gt=0)
    dispatch_max_attempts: int = Field(default=5, gt=0)
    dispatch_base_delay_seconds: float = Field(default=1.0, ge=0)
    dispatch_max_delay_seconds: float = Field(default=30.0, ge=0)
