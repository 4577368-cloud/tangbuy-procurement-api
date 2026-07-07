"""Tangbuy 采购履约 — FastAPI 后端入口。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.routers import (
    agent,
    auth,
    category_mapping,
    config,
    data_center,
    health,
    integrations,
    products,
    skill_audit,
    tasks,
)
from app.services.tasks.scheduler import start_task_auto_refresh, stop_task_auto_refresh


@asynccontextmanager
async def lifespan(_app: FastAPI):
    start_task_auto_refresh()
    yield
    stop_task_auto_refresh()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Tangbuy Procurement API",
        version="1.0.0",
        description="Tangbuy 智能采购履约系统 — 正式环境后端",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(tasks.router)
    app.include_router(agent.router)
    app.include_router(products.router)
    app.include_router(category_mapping.router)
    app.include_router(config.router)
    app.include_router(data_center.router)
    app.include_router(skill_audit.router)
    app.include_router(integrations.router)
    return app


app = create_app()
