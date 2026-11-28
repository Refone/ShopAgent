from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_LOG_DIR = PROJECT_ROOT / "var" / "logs" / "ai-service"

LogLevel = Annotated[
    Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    BeforeValidator(lambda value: str(value).strip().upper()),
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    # FastAPI
    ai_service_host: str = Field("127.0.0.1", description="服务的监听地址")
    ai_service_port: int = Field(18002, description="服务的监听端口")

    # Logging
    ai_service_log_dir: str = Field(
        "",
        description="日志文件输出目录，相对路径基于项目顶层目录；默认 var/logs/ai-service",
    )
    ai_service_log_file_level: LogLevel = Field(
        "DEBUG", description="写入文件的日志级别阈值"
    )
    ai_service_log_console_level: LogLevel = Field(
        "INFO", description="控制台（串口）输出的日志级别阈值"
    )
    ai_service_log_retention_days: int = Field(3, ge=1, description="日志文件保留天数")

    @property
    def log_dir(self) -> Path:
        """日志文件输出目录，未配置时使用项目顶层目录下的默认路径。"""
        configured = self.ai_service_log_dir.strip()
        directory = Path(configured).expanduser() if configured else DEFAULT_LOG_DIR
        if not directory.is_absolute():
            directory = PROJECT_ROOT / directory
        return directory


@lru_cache
def get_settings() -> Settings:
    return Settings()
