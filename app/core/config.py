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
    backend_cors_origins: str = (
        "http://localhost:3000,http://127.0.0.1:3000,"
        "http://localhost:3001,http://127.0.0.1:3001"
    )
    agent_work_root: str = ""
    llm_model_base_url: str = ""
    llm_model_api_key: str = ""
    llm_model_model_id: str = ""

    # Tangbuy Admin 订单读接口（listOrderDetail）
    tangbuy_admin_base_url: str = "https://admin.tangbuy.cc/prod-api"
    tangbuy_admin_token: str = ""
    tangbuy_admin_storage_no: int = 1
    tangbuy_admin_event_type_pending: int = 5888
    # 待采购人工放行 → 待支付；留空则仅写本地覆盖 + 审计，待接 Admin 写接口
    tangbuy_admin_procurement_pass_path: str = ""

    # Tangbuy Portal 商品详情（itemGet）
    tangbuy_portal_base_url: str = "https://www.tangbuy.cc/gateway"
    tangbuy_portal_token: str = ""
    tangbuy_portal_currency: str = "CNY"
    tangbuy_default_shipping_city: str = "广东惠州市"
    product_auto_pipeline: bool = True
    # 周期自动扫；0=关闭
    product_auto_scan_ms: int = 0
    product_auto_scan_batch: int = 3
    product_auto_scan_on_create: bool = False
    # 备选图搜日上限；0=不限制
    product_alt_scan_daily_limit: int = 0
    product_alt_scan_batch_size: int = 3

    @property
    def tangbuy_portal_configured(self) -> bool:
        return bool(self.tangbuy_portal_token.strip())

    @property
    def tangbuy_admin_configured(self) -> bool:
        from app.integrations.tangbuy_admin.token_store import resolve_admin_token

        t = resolve_admin_token().strip()
        return bool(t) and t != "your-admin-bearer-token"

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_model_base_url and self.llm_model_api_key and self.llm_model_model_id)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.backend_cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
