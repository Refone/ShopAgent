import asyncio
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from customer_service.common.settings import get_settings
from customer_service.infra.logger import LOG
from customer_service.models.pgsql import Base

async_engine: AsyncEngine = create_async_engine(
    get_settings().pgsql_url,
    echo=False,
)

session_factory = async_sessionmaker(
    async_engine,
    expire_on_commit=False,
)


async def get_async_db_session() -> AsyncGenerator[AsyncSession]:
    async with session_factory() as session:
        try:
            yield session
        finally:
            await session.close()


async def reset_database():
    async with async_engine.begin() as conn:
        LOG.info("数据库表准备重置")
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        LOG.info("数据库表重置完成")


if __name__ == "__main__":
    confirmation = input("确认重置数据库吗？(y/N): ").strip().lower()
    if confirmation in {"y", "yes"}:
        asyncio.run(reset_database())
    else:
        print("已取消数据库重置")
