from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


def _frontend_origins(host: str, port: int) -> list[str]:
    """Derive browser origins from a frontend bind address.

    Local binds also include localhost.
    """

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
    postgres_host: str = Field("127.0.0.1", description="PostgreSQL host.")
    postgres_port: int = Field(5432, description="PostgreSQL port.")
    postgres_user: str = Field("customer_service", description="PostgreSQL user name.")
    postgres_password: str = Field("customer_service", description="PostgreSQL password.")
    ecommerce_service_db: str = Field(
        "ecommerce", description="Database name used by ecommerce-service."
    )

    # JWT
    jwt_secret: str = Field("ecommerce-secret", description="JWT secret.")
    jwt_algorithm: str = Field("HS256", description="JWT signing and verification algorithm.")

    # Frontend (CORS allowlist is derived from the following addresses)
    user_frontend_host: str = Field("127.0.0.1", description="User frontend bind host.")
    user_frontend_port: int = Field(18100, description="User frontend bind port.")
    admin_frontend_host: str = Field("127.0.0.1", description="Admin frontend bind host.")
    admin_frontend_port: int = Field(18101, description="Admin frontend bind port.")

    @computed_field(description="Frontend origins allowed by CORS.")
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

    @computed_field(description="Database connection URL for ecommerce-service.")
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
