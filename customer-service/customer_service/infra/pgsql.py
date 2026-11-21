from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from customer_service.common.settings import get_settings

POSTGRESQL_ENGINE: AsyncEngine = create_async_engine(
    get_settings().pgsql_url,
    echo=False,
)

postgresql_session_factory = async_sessionmaker(
    POSTGRESQL_ENGINE,
    expire_on_commit=False,
)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    async with postgresql_session_factory() as session:
        yield session
