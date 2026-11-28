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
    ai_service_host: str = Field("127.0.0.1", description="Service bind host.")
    ai_service_port: int = Field(18002, description="Service bind port.")

    # Logging
    ai_service_log_dir: str = Field(
        "",
        description=(
            "Log file output directory; relative paths are resolved from the "
            "project root. Defaults to var/logs/ai-service."
        ),
    )
    ai_service_log_file_level: LogLevel = Field(
        "DEBUG", description="Minimum level for the file log handler."
    )
    ai_service_log_console_level: LogLevel = Field(
        "INFO", description="Minimum level for the console log handler."
    )
    ai_service_log_retention_days: int = Field(
        3, ge=1, description="Number of days to keep log files."
    )

    @property
    def log_dir(self) -> Path:
        """Log file output directory, falling back to the project default."""
        configured = self.ai_service_log_dir.strip()
        directory = Path(configured).expanduser() if configured else DEFAULT_LOG_DIR
        if not directory.is_absolute():
            directory = PROJECT_ROOT / directory
        return directory


@lru_cache
def get_settings() -> Settings:
    return Settings()
