from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

from pydantic import BeforeValidator, Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_LOG_DIR = PROJECT_ROOT / "var" / "logs" / "customer-service"

LogLevel = Annotated[
    Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    BeforeValidator(lambda value: str(value).strip().upper()),
]


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
    postgres_user: str = Field(
        "customer_service", description="PostgreSQL数据库的用户名"
    )
    postgres_password: str = Field(
        "customer_service", description="PostgreSQL数据库的密码"
    )
    customer_service_db: str = Field(
        "customer_service", description="customer-service 使用的数据库名"
    )

    # JWT
    jwt_secret: str = Field("ecommerce-secret", description="jwt秘钥")
    jwt_algorithm: str = Field("HS256", description="jwt加密以及解密的算法")

    # FastAPI
    customer_service_host: str = Field("0.0.0.0", description="服务的监听地址")
    customer_service_port: int = Field(8000, description="服务的监听端口")

    # Frontend（CORS 白名单由以下地址派生）
    user_frontend_host: str = Field("127.0.0.1", description="用户前端监听地址")
    user_frontend_port: int = Field(18100, description="用户前端监听端口")
    admin_frontend_host: str = Field("127.0.0.1", description="管理前端监听地址")
    admin_frontend_port: int = Field(18101, description="管理前端监听端口")

    # Conversation Settings
    conversation_idle_timeout_seconds: int = Field(
        600, description="AI模式下会话的有效时间, 超时会自动结束"
    )
    message_merge_delay_ms: int = Field(
        1000, description="AI模式下消息的防抖时间窗口阈值"
    )

    # Turn Worker Settings
    turn_worker_pull_interval_ms: int = Field(2000, description="Turn Worker 轮询间隔")

    # Logging
    customer_service_log_dir: str = Field(
        "",
        description="日志文件输出目录，相对路径基于项目顶层目录；默认 var/logs/customer-service",
    )
    customer_service_log_file_level: LogLevel = Field(
        "DEBUG", description="写入文件的日志级别阈值"
    )
    customer_service_log_console_level: LogLevel = Field(
        "INFO", description="控制台（串口）输出的日志级别阈值"
    )
    customer_service_log_retention_days: int = Field(
        3, ge=1, description="日志文件保留天数"
    )

    @property
    def log_dir(self) -> Path:
        """日志文件输出目录，未配置时使用项目顶层目录下的默认路径。"""
        configured = self.customer_service_log_dir.strip()
        directory = Path(configured).expanduser() if configured else DEFAULT_LOG_DIR
        if not directory.is_absolute():
            directory = PROJECT_ROOT / directory
        return directory

    @computed_field(description="允许跨域访问的前端地址")
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

    @computed_field(description="PostgreSQL数据库的地址")
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


@lru_cache
def get_settings() -> Settings:
    return Settings()


if __name__ == "__main__":
    settings = get_settings()

    print(settings.postgres_url)
