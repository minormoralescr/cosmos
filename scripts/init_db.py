"""Bootstrap de DB (dev): crea todas las tablas desde los modelos via create_all.

No requiere revisiones alembic. Para produccion, preferir el flujo alembic:
  alembic revision --autogenerate -m "initial"
  alembic upgrade head
"""
import asyncio

from app.db.base import Base, engine
import app.db.models  # noqa: F401 — registra modelos en Base.metadata


async def main() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("Tablas creadas (create_all) en", engine.url)


if __name__ == "__main__":
    asyncio.run(main())
