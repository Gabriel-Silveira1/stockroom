from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STOCKROOM_")

    database_url: str = "postgresql://stockroom:stockroom@localhost:5433/stockroom"
    rabbitmq_url: str = "amqp://stockroom:stockroom@localhost:5672/"
    db_pool_max_size: int = Field(default=10, gt=0)
    relay_idle_seconds: float = Field(default=0.2, gt=0)
    retry_delay_ms: int = Field(default=2000, gt=0)
    max_delivery_attempts: int = Field(default=5, gt=0)
    exchange_name: str = "stockroom.events"
    queue_prefix: str = ""
