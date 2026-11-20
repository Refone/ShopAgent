import logging

# ANSI 颜色定义
COLORS = {
    "DEBUG": "\033[37m",  # 灰色
    "INFO": "\033[32m",  # 绿色
    "WARNING": "\033[33m",  # 黄色
    "ERROR": "\033[31m",  # 红色
    "CRITICAL": "\033[1;31m",  # 红色加粗
}

RESET = "\033[0m"


class ColorFormatter(logging.Formatter):
    """彩色日志格式化器"""

    def format(self, record):
        # 复制 record，避免修改原始日志记录
        record = logging.makeLogRecord(record.__dict__)

        color = COLORS.get(record.levelname, "")
        record.levelname = f"{color}{record.levelname}{RESET}"

        return super().format(record)


def setup_logger(level=logging.DEBUG):
    """统一配置项目日志"""

    handler = logging.StreamHandler()
    handler.setFormatter(
        ColorFormatter(
            fmt=(
                "%(asctime)s | "
                "%(levelname)-17s | "
                "%(filename)s:%(lineno)d | "
                "%(funcName)s() | "
                "%(message)s"
            ),
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # 避免重复添加 Handler
    if not any(
        isinstance(h, logging.StreamHandler) and getattr(h, "_color_handler", False)
        for h in root_logger.handlers
    ):
        handler._color_handler = True
        root_logger.addHandler(handler)


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
