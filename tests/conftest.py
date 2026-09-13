"""Shared pytest fixtures for PostgreSQL database testing."""

import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://platform_user:platform_pass@localhost:5432/transaction_platform",
)


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """Function-scoped database engine for loop-safe asyncpg testing.

    Fails with a clear setup error if the database is unavailable.
    """
    engine = create_async_engine(DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        raise RuntimeError(
            f"Failed to connect to PostgreSQL database at {DATABASE_URL}. "
            "Ensure PostgreSQL is running via `docker compose up -d`."
        ) from exc

    yield engine
    await engine.dispose()


@pytest.fixture
async def clean_database(engine: AsyncEngine) -> AsyncIterator[None]:
    """Truncate tables before each test for clean isolation."""
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        async with session.begin():
            await session.execute(
                text("TRUNCATE TABLE idempotency_records, transactions CASCADE")
            )
    yield


@pytest.fixture
def session_factory(
    engine: AsyncEngine, clean_database: None
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
