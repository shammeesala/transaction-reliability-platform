"""Database engine, session factory, and configuration."""

import os
from collections.abc import AsyncIterator

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool


class DatabaseSettings(BaseSettings):
    """Database connection settings."""

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)

    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://platform_user:platform_pass@localhost:5432/transaction_platform",
    )
    pool_size: int = 10
    max_overflow: int = 20
    echo: bool = False
    use_null_pool: bool = False


def create_engine_and_session_factory(
    settings: DatabaseSettings,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Create SQLAlchemy async engine and session factory."""
    if settings.use_null_pool or settings.pool_size == 0:
        engine = create_async_engine(
            settings.database_url,
            poolclass=NullPool,
            echo=settings.echo,
        )
    else:
        engine = create_async_engine(
            settings.database_url,
            pool_size=settings.pool_size,
            max_overflow=settings.max_overflow,
            echo=settings.echo,
        )
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    return engine, session_factory


async def get_db_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Dependency provider for an async database session."""
    async with session_factory() as session:
        yield session
