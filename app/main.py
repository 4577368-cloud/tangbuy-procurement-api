"""Tangbuy 采购履约 — FastAPI 后端入口。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.bootstrap_env import load_env_local

load_env_local()

from app.core.config import get_settings
from app.routers import (
    agent,
    auth,
    category_mapping,
    config,
    data_center,
    evolution,
    health,
    integrations,
    orders,
    products,
    skill_audit,
    tasks,
)
from app.services.tasks.scheduler import start_task_auto_refresh, stop_task_auto_refresh
from app.services.products.product_jobs import start_product_auto_scan, stop_product_auto_scan


@asynccontextmanager
async def lifespan(_app: FastAPI):
    start_task_auto_refresh()
    start_product_auto_scan()
    try:
        from app.services.products.product_jobs import resume_stale_enrichments

        resume_stale_enrichments(limit=80)
    except Exception:
        pass
    yield
    stop_product_auto_scan()
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

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
        if isinstance(exc, HTTPException):
            raise exc
        return JSONResponse(
            status_code=500,
            content={"detail": str(exc) or "Internal Server Error"},
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
    app.include_router(evolution.router)
    app.include_router(integrations.router)
    app.include_router(orders.router)
    return app


app = create_app()
