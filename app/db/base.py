"""Base declarativa + engine async + factory de sesiones.

Punto unico de configuracion del motor (DRY): el resto de la app importa
`Base`, `engine`, `async_session_factory` y `get_session` desde aqui.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    """Raiz declarativa unica para todos los modelos (metadata compartido)."""


engine = create_async_engine(
    settings.database_url,
    echo=settings.db_echo,
    pool_pre_ping=True,  # descarta conexiones stalidas tras reciclar el sandbox/DB
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,  # permite usar objetos tras commit sin refresh
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency de FastAPI: yield de sesion transaccional."""
    async with async_session_factory() as session:
        yield session
