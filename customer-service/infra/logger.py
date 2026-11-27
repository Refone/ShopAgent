from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path

from common.settings import get_settings

# ANSI 颜色定义
COLORS = {
    "DEBUG": "\033[37m",  # 灰色
    "INFO": "\033[34m",  # 蓝色
    "WARNING": "\033[38;5;208m",  # 橙色
    "ERROR": "\033[31m",  # 红色
    "CRITICAL": "\033[1;31m",  # 红色加粗
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


class ColorFormatter(logging.Formatter):
    """彩色日志格式化器"""

    def format(self, record: logging.LogRecord) -> str:
        # 复制 record，避免修改原始日志记录
        record = logging.makeLogRecord(record.__dict__)

        color = COLORS.get(record.levelname, "")
        record.levelname = f"{color}{record.levelname}{RESET}"

        return super().format(record)


class DailyFileHandler(logging.FileHandler):
    """按自然日写入 <YYYY-MM-DD>.log 的文件处理器。

    写入前检查日期，跨天后切换到新文件，并在启动和跨天时清理过期日志。
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
        """删除保留期之外的日志文件，保留最近 N 个自然日（含当天）。"""
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
        raise ValueError(f"未知日志级别: {name!r}")
    return level


def _find_handler(logger: logging.Logger, marker: str) -> logging.Handler | None:
    for handler in logger.handlers:
        if getattr(handler, marker, False):
            return handler
    return None


def setup_logger() -> None:
    """根据配置初始化控制台与按天文件两个日志通道。"""

    settings = get_settings()
    file_level = _resolve_level(settings.customer_service_log_file_level)
    console_level = _resolve_level(settings.customer_service_log_console_level)

    root_logger = logging.getLogger()
    # 先按两个通道中更低的阈值放行，再由各 Handler 自己过滤
    root_logger.setLevel(min(file_level, console_level))

    console_handler = _find_handler(root_logger, "_console_handler")
    if console_handler is None:
        console_handler = logging.StreamHandler()
        console_handler._console_handler = True
        root_logger.addHandler(console_handler)
    console_handler.setLevel(console_level)
    console_handler.setFormatter(
        ColorFormatter(fmt=CONSOLE_FORMAT, datefmt=DATE_FORMAT)
    )

    file_handler = _find_handler(root_logger, "_file_handler")
    if file_handler is None:
        try:
            file_handler = DailyFileHandler(
                directory=settings.log_dir,
                retention_days=settings.customer_service_log_retention_days,
            )
        except (OSError, ValueError) as exc:
            root_logger.warning(
                "无法创建日志目录 %s，已跳过文件日志: %s", settings.log_dir, exc
            )
            return

        file_handler._file_handler = True
        root_logger.addHandler(file_handler)

    file_handler.setLevel(file_level)
    file_handler.setFormatter(logging.Formatter(fmt=FILE_FORMAT, datefmt=DATE_FORMAT))


# 初始化
setup_logger()

# 创建项目 logger
LOG = logging.getLogger("app")

if __name__ == "__main__":
    LOG.debug("这是一条 DEBUG 调试信息")
    LOG.info("这是一条 INFO 正常运行信息")
    LOG.warning("这是一条 WARNING 警告信息")
    LOG.error("这是一条 ERROR 错误信息")
    LOG.critical("这是一条 CRITICAL 严重错误信息")

    print("\n========== 日志测试完成 ==========\n")

    # 测试异常日志
    try:
        result = 1 / 0
    except Exception:
        LOG.exception("捕获到一个异常")
