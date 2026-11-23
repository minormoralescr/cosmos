"""Fixtures compartidas para tests de integracion (PostgreSQL real).

Si no hay Postgres alcanzable, los tests de integracion se SKIPEAN
(no se simulan — Directiva #4: cero mocks de logica de negocio).
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db.base import Base
import app.db.models  # noqa: F401 — registra modelos en el metadata


@pytest_asyncio.fixture
async def db_engine():
    """Engine real. Falla rapido (timeout 3s) si no hay Postgres -> skip."""
    engine = create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args={"timeout": 3},
    )
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"PostgreSQL no disponible: {exc.__class__.__name__}")
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    """Sesion aislada: recrea el schema por test y hace rollback al final."""
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(
        db_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as session:
        yield session
        await session.rollback()
        await session.close()
