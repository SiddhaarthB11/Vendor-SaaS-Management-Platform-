from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql://slmct_user:slmct_password@localhost:5432/slmct"
    allowed_origins: list[str] = ["http://localhost:3002", "http://127.0.0.1:3002"]
    gemini_api_key: str = ""
    notdiamond_api_key: str = ""
    notdiamond_routing_enabled: bool = False
    notdiamond_optimize_enabled: bool = False
    copilot_mode: str = "tools"
    app_base_url: str = "http://localhost:3002"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
