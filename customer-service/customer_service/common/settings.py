from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

from pydantic import BeforeValidator, Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_LOG_DIR = PROJECT_ROOT / "var" / "logs" / "customer-service"

LogLevel = Annotated[
    Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    BeforeValidator(lambda value: str(value).strip().upper()),
]


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
    postgres_password: str = Field(
        "customer_service", description="PostgreSQL password."
    )
    customer_service_db: str = Field(
        "customer_service", description="Database name used by customer-service."
    )

    # JWT
    jwt_secret: str = Field("ecommerce-secret", description="JWT secret.")
    jwt_algorithm: str = Field(
        "HS256", description="JWT signing and verification algorithm."
    )

    # FastAPI
    customer_service_host: str = Field("0.0.0.0", description="Service bind host.")
    customer_service_port: int = Field(8000, description="Service bind port.")

    # Frontend (CORS allowlist is derived from the following addresses)
    user_frontend_host: str = Field("127.0.0.1", description="User frontend bind host.")
    user_frontend_port: int = Field(18100, description="User frontend bind port.")
    admin_frontend_host: str = Field(
        "127.0.0.1", description="Admin frontend bind host."
    )
    admin_frontend_port: int = Field(18101, description="Admin frontend bind port.")

    # Conversation Settings
    conversation_idle_timeout_seconds: int = Field(
        600,
        description=(
            "Idle timeout for AI conversations; expired conversations are closed "
            "automatically."
        ),
    )
    message_merge_delay_ms: int = Field(
        1000, description="Debounce window for messages in AI conversations."
    )

    # Turn Worker Settings
    turn_worker_pull_interval_ms: int = Field(
        2000, description="Polling interval for the turn worker."
    )

    # Logging
    customer_service_log_dir: str = Field(
        "",
        description=(
            "Log file output directory; relative paths are resolved from the "
            "project root. Defaults to var/logs/customer-service."
        ),
    )
    customer_service_log_file_level: LogLevel = Field(
        "DEBUG", description="Minimum level for the file log handler."
    )
    customer_service_log_console_level: LogLevel = Field(
        "INFO", description="Minimum level for the console log handler."
    )
    customer_service_log_retention_days: int = Field(
        3, ge=1, description="Number of days to keep log files."
    )

    ai_timeout_seconds: int = Field(
        30, description="Timeout for requests to the AI service, in seconds."
    )
    ai_service_host: str = Field("127.0.0.1", description="AI service host.")
    ai_service_port: int = Field(18002, description="AI service port.")

    @property
    def log_dir(self) -> Path:
        """Log file output directory, falling back to the project default."""
        configured = self.customer_service_log_dir.strip()
        directory = Path(configured).expanduser() if configured else DEFAULT_LOG_DIR
        if not directory.is_absolute():
            directory = PROJECT_ROOT / directory
        return directory

    @computed_field(description="Frontend origins allowed by CORS.")
    @property
    def cors_origins(self) -> list[str]:
        return list(
            dict.fromkeys(
                [
                    *_frontend_origins(
                        self.user_frontend_host, self.user_frontend_port
                    ),
                    *_frontend_origins(
                        self.admin_frontend_host, self.admin_frontend_port
                    ),
                ]
            )
        )

    @computed_field(description="PostgreSQL connection URL.")
    @property
    def postgres_url(self) -> str:
        user = quote(self.postgres_user, safe="")
        password = quote(self.postgres_password, safe="")
        database = quote(self.customer_service_db, safe="")
        host = self.postgres_host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"

        return (
            f"postgresql+psycopg://{user}:{password}"
            f"@{host}:{self.postgres_port}/{database}"
        )

    @computed_field(description="AI service URL.")
    @property
    def ai_service_url(self) -> str:
        return f"http://{self.ai_service_host}:{self.ai_service_port}"


@lru_cache
def get_settings() -> Settings:
    return Settings()


if __name__ == "__main__":
    settings = get_settings()

    print(settings.postgres_url)
    print(settings.ai_service_url)
