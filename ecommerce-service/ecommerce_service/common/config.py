from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


def _frontend_origins(host: str, port: int) -> list[str]:
    """根据前端监听地址派生浏览器 Origin，本地监听时兼容 localhost。"""

    hosts = {host}
    if host in {"127.0.0.1", "localhost"}:
        hosts = {"127.0.0.1", "localhost"}
    elif host in {"", "0.0.0.0", "::", "[::]"}:
        # 监听所有网卡时，浏览器通常仍通过本机回环地址访问
        hosts = {"127.0.0.1", "localhost"}

    origins = []
    for item in sorted(hosts):
        if ":" in item and not item.startswith("["):
            item = f"[{item}]"
        origins.append(f"http://{item}:{port}")
    return origins


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    # PostgreSQL
    postgres_host: str = Field("127.0.0.1", description="PostgreSQL数据库的主机地址")
    postgres_port: int = Field(5432, description="PostgreSQL数据库的端口")
    postgres_user: str = Field("customer_service", description="PostgreSQL数据库的用户名")
    postgres_password: str = Field("customer_service", description="PostgreSQL数据库的密码")
    ecommerce_service_db: str = Field("ecommerce", description="ecommerce-service 使用的数据库名")

    # JWT
    jwt_secret: str = Field("ecommerce-secret", description="jwt秘钥")
    jwt_algorithm: str = Field("HS256", description="jwt加密以及解密的算法")

    # Frontend（CORS 白名单由以下地址派生）
    user_frontend_host: str = Field("127.0.0.1", description="用户前端监听地址")
    user_frontend_port: int = Field(18100, description="用户前端监听端口")
    admin_frontend_host: str = Field("127.0.0.1", description="管理前端监听地址")
    admin_frontend_port: int = Field(18101, description="管理前端监听端口")

    @computed_field(description="允许跨域访问的前端地址")
    @property
    def cors_origins(self) -> list[str]:
        return list(
            dict.fromkeys(
                [
                    *_frontend_origins(self.user_frontend_host, self.user_frontend_port),
                    *_frontend_origins(self.admin_frontend_host, self.admin_frontend_port),
                ]
            )
        )

    @computed_field(description="ecommerce-service 的数据库连接地址")
    @property
    def ecom_database_url(self) -> str:
        user = quote(self.postgres_user, safe="")
        password = quote(self.postgres_password, safe="")
        database = quote(self.ecommerce_service_db, safe="")
        host = self.postgres_host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"

        return f"postgresql+psycopg://{user}:{password}@{host}:{self.postgres_port}/{database}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
