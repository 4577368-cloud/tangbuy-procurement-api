"""数据库引擎与会话。"""

from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from typing import Generator, Optional

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from app.core.config import get_settings


def database_url() -> str:
    return (get_settings().database_url or "").strip()


def is_db_enabled() -> bool:
    return bool(database_url())


@lru_cache
def get_engine() -> Optional[Engine]:
    url = database_url()
    if not url:
        return None
    if url.startswith("sqlite"):
        # 本地 SQLite：有界 QueuePool + check_same_thread=False。
        # 每个连接同一时刻只归一个 session/线程使用（不像 StaticPool 多线程共用单连接崩溃），
        # 又能复用连接（不像 NullPool 每次新建 + 跑 PRAGMA，批量请求会开上千连接打满线程池）。
        engine = create_engine(
            url,
            connect_args={"check_same_thread": False, "timeout": 30},
            poolclass=QueuePool,
            pool_size=8,
            max_overflow=16,
            pool_timeout=30,
            pool_recycle=1800,
            pool_pre_ping=True,
        )

        @event.listens_for(engine, "connect")
        def _sqlite_pragma(dbapi_connection, _connection_record) -> None:  # noqa: N806
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

        return engine

    engine = create_engine(
        url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        pool_timeout=60,
    )
    return engine


@lru_cache
def get_session_factory() -> Optional[sessionmaker[Session]]:
    engine = get_engine()
    if engine is None:
        return None
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@contextmanager
def db_session() -> Generator[Session, None, None]:
    factory = get_session_factory()
    if factory is None:
        raise RuntimeError("DATABASE_URL 未配置")
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_db_connection() -> dict:
    if not is_db_enabled():
        return {"ok": False, "enabled": False, "error": "DATABASE_URL 未配置"}
    engine = get_engine()
    if engine is None:
        return {"ok": False, "enabled": True, "error": "engine 未初始化"}
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"ok": True, "enabled": True, "url_scheme": engine.url.get_backend_name()}
    except Exception as exc:
        return {"ok": False, "enabled": True, "error": str(exc)}


def init_database() -> None:
    """校验数据库连接（表结构由 Alembic 管理）。"""
    if not is_db_enabled():
        return
    engine = get_engine()
    if engine is not None and engine.url.get_backend_name() == "sqlite":
        try:
            with engine.connect() as conn:
                conn.execute(text("PRAGMA journal_mode=WAL"))
                conn.execute(text("PRAGMA busy_timeout=30000"))
        except Exception:
            pass
    check_db_connection()
