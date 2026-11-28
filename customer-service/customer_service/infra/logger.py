from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path

from customer_service.common.settings import get_settings

# ANSI color codes
COLORS = {
    "DEBUG": "\033[37m",  # Gray
    "INFO": "\033[34m",  # Blue
    "WARNING": "\033[38;5;208m",  # Orange
    "ERROR": "\033[31m",  # Red
    "CRITICAL": "\033[1;31m",  # Bold red
}

RESET = "\033[0m"
YELLOW = "\033[33m"

LOG_FILE_DATE_FORMAT = "%Y-%m-%d"
LOG_FILE_SUFFIX = ".log"

CONSOLE_FORMAT = (
    f"{YELLOW}%(asctime)s{RESET} | "
    "%(levelname)-17s | "
    "%(filename)s:%(lineno)d | "
    "%(funcName)s() | "
    "%(message)s"
)
FILE_FORMAT = (
    "%(asctime)s | "
    "%(levelname)-8s | "
    "%(filename)s:%(lineno)d | "
    "%(funcName)s() | "
    "%(message)s"
)
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 业务日志专用 logger。Handler 只挂在它上面，不改动 root：
# 第三方库（如 httpcore/httpx）的日志仍冒泡到默认 WARNING 的 root，
# 它们的 DEBUG/INFO 在 isEnabledFor 阶段就会被丢弃。
PROJECT_LOGGER_NAME = "app"


class ColorFormatter(logging.Formatter):
    """Colorized console log formatter."""

    def format(self, record: logging.LogRecord) -> str:
        # 复制 record，避免修改原始日志记录
        record = logging.makeLogRecord(record.__dict__)

        color = COLORS.get(record.levelname, "")
        record.levelname = f"{color}{record.levelname}{RESET}"

        return super().format(record)


class DailyFileHandler(logging.FileHandler):
    """File handler that writes to daily log files named <YYYY-MM-DD>.log.

    The date is checked before each write; after midnight the handler switches
    to a new file and cleans up expired log files on startup and rollover.
    """

    def __init__(
        self,
        directory: str | Path,
        retention_days: int = 3,
        encoding: str = "utf-8",
    ) -> None:
        self.directory = Path(directory)
        self.retention_days = retention_days
        self.directory.mkdir(parents=True, exist_ok=True)

        self._current_date = self._today()
        self._cleanup_expired()

        super().__init__(
            filename=self.directory / f"{self._current_date}{LOG_FILE_SUFFIX}",
            mode="a",
            encoding=encoding,
            delay=False,
        )

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime(LOG_FILE_DATE_FORMAT)

    def emit(self, record: logging.LogRecord) -> None:
        self._rollover_if_needed()
        super().emit(record)

    def _rollover_if_needed(self) -> None:
        today = self._today()
        if today == self._current_date:
            return

        if self.stream is not None:
            try:
                self.stream.close()
            finally:
                self.stream = None

        self._current_date = today
        self.baseFilename = os.path.abspath(
            self.directory / f"{today}{LOG_FILE_SUFFIX}"
        )
        self._cleanup_expired()

    def _cleanup_expired(self) -> None:
        """Delete log files outside the retention window.

        Keep the latest N days, including today.
        """
        if self.retention_days < 1:
            return

        cutoff = date.today() - timedelta(days=self.retention_days - 1)
        for path in self.directory.glob(f"*{LOG_FILE_SUFFIX}"):
            file_date = self._parse_file_date(path.stem)
            if file_date is None or file_date >= cutoff:
                continue

            try:
                path.unlink()
            except OSError:
                # 清理失败不应影响正常日志写入
                pass

    @staticmethod
    def _parse_file_date(stem: str) -> date | None:
        try:
            return datetime.strptime(stem, LOG_FILE_DATE_FORMAT).date()
        except ValueError:
            return None


def _resolve_level(name: str) -> int:
    level = logging.getLevelNamesMapping().get(name.strip().upper())
    if level is None:
        raise ValueError(f"Unknown log level: {name!r}")
    return level


def _find_handler(logger: logging.Logger, marker: str) -> logging.Handler | None:
    for handler in logger.handlers:
        if getattr(handler, marker, False):
            return handler
    return None


def setup_logger() -> None:
    """Initialize the console and daily-file log handlers from settings."""

    settings = get_settings()
    file_level = _resolve_level(settings.customer_service_log_file_level)
    console_level = _resolve_level(settings.customer_service_log_console_level)

    project_logger = logging.getLogger(PROJECT_LOGGER_NAME)
    # 先按两个通道中更低的阈值放行，再由各 Handler 自己过滤
    project_logger.setLevel(min(file_level, console_level))
    # 业务日志不向 root 冒泡，避免混入第三方库日志或重复输出
    project_logger.propagate = False

    console_handler = _find_handler(project_logger, "_console_handler")
    if console_handler is None:
        console_handler = logging.StreamHandler()
        console_handler._console_handler = True
        project_logger.addHandler(console_handler)
    console_handler.setLevel(console_level)
    console_handler.setFormatter(
        ColorFormatter(fmt=CONSOLE_FORMAT, datefmt=DATE_FORMAT)
    )

    file_handler = _find_handler(project_logger, "_file_handler")
    if file_handler is None:
        try:
            file_handler = DailyFileHandler(
                directory=settings.log_dir,
                retention_days=settings.customer_service_log_retention_days,
            )
        except (OSError, ValueError) as exc:
            project_logger.warning(
                f"Failed to create log directory {settings.log_dir}, "
                f"skipping file logging: {exc}"
            )
            return

        file_handler._file_handler = True
        project_logger.addHandler(file_handler)

    file_handler.setLevel(file_level)
    file_handler.setFormatter(logging.Formatter(fmt=FILE_FORMAT, datefmt=DATE_FORMAT))


# Setup logging on import
setup_logger()

# Project logger
LOG = logging.getLogger(PROJECT_LOGGER_NAME)

if __name__ == "__main__":
    LOG.debug("This is a DEBUG message.")
    LOG.info("This is an INFO message.")
    LOG.warning("This is a WARNING message.")
    LOG.error("This is an ERROR message.")
    LOG.critical("This is a CRITICAL message.")

    LOG.info("Log test finished.")

    # Test exception logging
    try:
        result = 1 / 0
    except Exception:
        LOG.exception("Caught an exception during logging test.")
