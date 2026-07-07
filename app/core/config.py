"""环境配置（与 .env.local 字段对齐）。"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.paths import PROJECT_ROOT


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env.local", PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    auth_session_secret: str = "tangbuy-dev-session-secret"
    backend_cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    agent_work_root: str = ""
    llm_model_base_url: str = ""
    llm_model_api_key: str = ""
    llm_model_model_id: str = ""

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_model_base_url and self.llm_model_api_key and self.llm_model_model_id)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.backend_cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
