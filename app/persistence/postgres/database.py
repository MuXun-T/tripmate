from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings, get_settings
from app.domain.context import RequestContext


def affected_rows(result: Any) -> int:
    """Read the affected-row count from SQLAlchemy's text execution result."""
    return int(result.rowcount)


def create_engine(settings: Settings | None = None) -> AsyncEngine:
    config = settings or get_settings()
    return create_async_engine(
        config.database_url,
        pool_size=config.db_pool_max,
        max_overflow=max(0, config.db_pool_max - config.db_pool_min),
        pool_pre_ping=True,
        connect_args={
            "server_settings": {"statement_timeout": str(config.db_statement_timeout_ms)}
        },
    )


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def scoped_transaction(
    factory: async_sessionmaker[AsyncSession], ctx: RequestContext
) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": str(ctx.tenant_id)},
            )
            yield session


async def check_database(engine: AsyncEngine) -> bool:
    async with engine.connect() as connection:
        return bool((await connection.scalar(text("SELECT 1"))) == 1)
