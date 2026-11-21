from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


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
