from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from customer_service.common.config import get_settings
from customer_service.common.event_loop import run_async

__all__ = ["DB_ENGINE", "get_db_session"]

DB_ENGINE: AsyncEngine = create_async_engine(
    url=get_settings().database_url,
    echo=False,  # 打印 SQL 日志，方便调试
)

session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=DB_ENGINE,
    # 上一步查询的数据是否还保留在内存中
    # 如果设置为 True，提交后对象会从内存中失效，需要重新查询才能获取最新数据
    # User = 查询出来的数据
    # commit
    # user.name 查不到
    expire_on_commit=False,  # 用异步数据库连接时，这个参数一定要设置为 False
)


async def get_db_session() -> AsyncGenerator[AsyncSession]:
    """
    1. 调用者：fastapi 在处理陆游请求时调用
    2. 方法返回的 session 要用 yield, 不能 return
    """

    async with session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


if __name__ == "__main__":

    async def get_db_session_test():
        async for session in get_db_session():
            ressult = await session.execute(text("SELECT 1"))
            print(ressult.scalar())

    # Linux / MacOS:
    # asyncio.run(get_db_session_test())

    # Windows:
    run_async(get_db_session_test())
