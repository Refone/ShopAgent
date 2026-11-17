import asyncio

from customer_service.infra.db import DB_ENGINE
from customer_service.models.conversation import Base


async def init_database() -> None:
    """
    初始化数据库，创建所有表
    """

    async with DB_ENGINE.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


if __name__ == "__main__":
    # Linux / MacOS:
    asyncio.run(init_database())

    # Windows:
    # run_async(init_database())
