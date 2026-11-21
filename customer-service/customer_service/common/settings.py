from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    # PostgreSQL
    pgsql_host: str = Field("127.0.0.1", description="PgSQL数据库的主机地址")
    pgsql_port: int = Field(5432, description="PgSQL数据库的端口")
    pgsql_user: str = Field("customer_service", description="PgSQL数据库的用户名")
    pgsql_password: str = Field("customer_service", description="PgSQL数据库的密码")
    pgsql_db: str = Field("customer_service", description="PgSQL数据库的数据库名")

    # JWT
    jwt_secret: str = Field("ecommerce-secret", description="jwt秘钥")
    jwt_algorithm: list[str] = Field(["HS256"], description="jwt加密以及解密的算法")

    # FastAPI
    api_host: str = Field("0.0.0.0", description="服务的访问域名")
    api_port: int = Field(8000, description="服务的访问端口")
    cors_origins: list[str] = Field(
        [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:5174",
            "http://127.0.0.1:5174",
        ],
        description="服务端允许的源",
    )

    # Conversation Settings
    conversation_idle_timeout_seconds: int = Field(
        600, description="AI模式下会话的有效时间, 超时会自动结束"
    )
    message_merge_delay_ms: int = Field(
        1000, description="AI模式下消息的防抖时间窗口阈值"
    )

    # Turn Worker Settings
    turn_worker_pull_interval_ms: int = Field(2000, description="Turn Worker 轮询间隔")

    @computed_field(description="PgSQL数据库的地址")
    @property
    def pgsql_url(self) -> str:
        user = quote(self.pgsql_user, safe="")
        password = quote(self.pgsql_password, safe="")
        database = quote(self.pgsql_db, safe="")
        host = self.pgsql_host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"

        return (
            f"postgresql+psycopg://{user}:{password}"
            f"@{host}:{self.pgsql_port}/{database}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


if __name__ == "__main__":
    settings = get_settings()

    print(settings.pgsql_url)
