import argparse
import asyncio

from sqlalchemy import text

from customer_service.infra.pgsql import POSTGRESQL_ENGINE
from customer_service.schemas.entities import Base


async def reset_database() -> None:
    async with POSTGRESQL_ENGINE.begin() as conn:
        print("正在清空数据库...")
        # 直接重建 public schema，避免旧表遗留的外键依赖导致 drop_all 删除顺序出错
        await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        print("正在创建数据库表...")
        await conn.run_sync(Base.metadata.create_all)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="重置 customer-service 数据库")
    parser.add_argument("-y", "--yes", action="store_true", help="跳过确认提示")
    args = parser.parse_args()

    if not args.yes and input("确认重置数据库吗？(y/N): ").strip().lower() not in {
        "y",
        "yes",
    }:
        print("已取消数据库重置")
    else:
        asyncio.run(reset_database())
        print("数据库重置完成")
