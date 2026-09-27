from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class InventorySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STOCKROOM_")

    database_url: str = "postgresql://stockroom:stockroom@localhost:5433/stockroom"
    reservation_ttl_seconds: int = Field(default=900, gt=0)
    db_pool_max_size: int = Field(default=10, gt=0)
