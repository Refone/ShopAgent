"""ShopAgent 一键式部署脚本。

不带参数运行时进入 ASCII 交互式控制台；带子命令时用于 CI 等非交互场景，例如：

    python deploy.py deploy
    python deploy.py build frontend
    python deploy.py status --strict
    python deploy.py help
"""

from __future__ import annotations

import argparse
import os
import re
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import termios
import time
import tty
import unicodedata
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Self

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"

ECOMMERCE_DIR = ROOT / "ecommerce-service"
CUSTOMER_DIR = ROOT / "customer-service"
AI_DIR = ROOT / "ai-service"
FRONTEND_DIR = ROOT / "frontend"
USER_FRONTEND_DIR = FRONTEND_DIR / "user-frontend"
ADMIN_FRONTEND_DIR = FRONTEND_DIR / "admin-frontend"

COMPOSE_FILE = ECOMMERCE_DIR / "compose.yml"
RUN_DIR = ROOT / "var" / "run"
DEPLOY_LOG_DIR = ROOT / "var" / "logs" / "deploy"
HELP_FILE = ROOT / "docs" / "deploy" / "help.txt"

ALL_COMPONENTS = ("ecommerce", "customer", "ai", "frontend")
COMPONENT_ALIASES = {
    "ecommerce-service": "ecommerce",
    "customer-service": "customer",
    "ai-service": "ai",
    "frontends": "frontend",
}

DEFAULT_PORTS = {
    "POSTGRES_PORT": 18200,
    "REDIS_PORT": 18201,
    "CUSTOMER_SERVICE_PORT": 18000,
    "ECOMMERCE_SERVICE_PORT": 18001,
    "AI_SERVICE_PORT": 18002,
    "USER_FRONTEND_PORT": 18100,
    "ADMIN_FRONTEND_PORT": 18101,
}

REQUIRED_ENV = (
    "JWT_SECRET",
    "JWT_ALGORITHM",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "CUSTOMER_SERVICE_DB",
    "ECOMMERCE_SERVICE_DB",
    "CUSTOMER_SERVICE_HOST",
    "CUSTOMER_SERVICE_PORT",
    "ECOMMERCE_SERVICE_HOST",
    "ECOMMERCE_SERVICE_PORT",
    "AI_SERVICE_HOST",
    "AI_SERVICE_PORT",
    "REDIS_PORT",
    "USER_FRONTEND_HOST",
    "USER_FRONTEND_PORT",
    "ADMIN_FRONTEND_HOST",
    "ADMIN_FRONTEND_PORT",
)

CREATE_CUSTOMER_TABLES_CODE = """
import asyncio

from customer_service.infra.pgsql import POSTGRESQL_ENGINE
from customer_service.schemas.entities import Base


async def main() -> None:
    async with POSTGRESQL_ENGINE.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


asyncio.run(main())
"""

_COLOR_ENABLED = (
    sys.stdout.isatty()
    and os.environ.get("NO_COLOR") is None
    and os.environ.get("TERM", "") != "dumb"
)

_HTTP_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

_ENV_CACHE: dict[str, str] | None = None
_DOCKER_READY = False


class DeployError(RuntimeError):
    """可预期的部署错误，由入口统一打印。"""


def _paint(text: str, code: str) -> str:
    """为文本添加 ANSI 颜色，非 TTY 时原样返回。"""
    return f"\033[{code}m{text}\033[0m" if _COLOR_ENABLED else text


def _section(title: str) -> None:
    """输出命令行的分节标题。"""
    print(f"\n{_paint(f'== {title} ==', '1;36')}")


def _info(message: str) -> None:
    """输出普通部署日志。"""
    print(f"{_paint('[deploy]', '36')} {message}")


def _ok(message: str) -> None:
    """输出成功日志。"""
    print(f"{_paint('[  ok  ]', '32')} {message}")


def _warn(message: str) -> None:
    """输出警告日志。"""
    print(f"{_paint('[ warn ]', '33')} {message}")


def _error(message: str) -> None:
    """输出错误日志到 stderr。"""
    print(f"{_paint('[ fail ]', '31')} {message}", file=sys.stderr)


def _rel(path: Path) -> str:
    """返回相对仓库根目录的路径；无法相对时返回绝对路径。"""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


@dataclass(frozen=True)
class _Frontend:
    name: str
    directory: Path
    env_prefix: str
    default_port: int


_FRONTENDS = (
    _Frontend("user-frontend", USER_FRONTEND_DIR, "USER_FRONTEND", 18100),
    _Frontend("admin-frontend", ADMIN_FRONTEND_DIR, "ADMIN_FRONTEND", 18101),
)


@dataclass(frozen=True)
class _Check:
    label: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class _ServiceInfo:
    key: str
    label: str
    host: str
    port: int
    url: str
    state: str
    detail: str
    pid: int | None = None
    implemented: bool = True

    @property
    def running(self) -> bool:
        """服务是否已通过 HTTP 健康检查。"""
        return self.state in {"running", "external"}


@dataclass(frozen=True)
class _PortInfo:
    label: str
    port: int
    in_use: bool


@dataclass(frozen=True)
class _ActionInfo:
    key: str
    label: str
    detail: str
    confirm: str | None = None


@dataclass
class _Diagnostics:
    checks: list[_Check]
    services: list[_ServiceInfo]
    ports: list[_PortInfo]
    configured: bool = True


@dataclass
class _ConsoleRow:
    text: str
    key: str | None = None


_ACTIONS = (
    _ActionInfo(
        "build_all",
        "Build all",
        "Build ecommerce, customer and both frontends",
    ),
    _ActionInfo(
        "deploy_all",
        "Deploy all",
        "Build and start every service",
    ),
    _ActionInfo(
        "reset_db",
        "Reset databases",
        "Drop PostgreSQL volume and Redis data, then seed demo data",
        "Reset all databases? This cannot be undone.",
    ),
    _ActionInfo("quit", "Quit", "Leave the console"),
)


# ---------------------------------------------------------------------------
# 环境变量与配置
# ---------------------------------------------------------------------------


def _parse_env_line(line: str) -> tuple[str, str] | None:
    """解析一行 .env 配置。

    示例：'PORT=18000' -> ('PORT', '18000')；'# comment' -> None。
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].lstrip()
    if "=" not in stripped:
        return None

    key, value = stripped.split("=", 1)
    key = key.strip()
    value = value.strip()
    if not key:
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    elif " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return key, value


def _read_env_file(path: Path = ENV_FILE) -> dict[str, str]:
    """读取 .env 为字典；示例：文件存在 -> {'PORT': '18000'}，不存在 -> {}。"""
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_env_line(line)
        if parsed is not None:
            values[parsed[0]] = parsed[1]
    return values


def load_env(refresh: bool = False) -> dict[str, str]:
    """读取 .env，并让进程环境变量覆盖文件中的同名配置。"""

    global _ENV_CACHE
    if _ENV_CACHE is None or refresh:
        values = _read_env_file()
        values.update(os.environ)
        _ENV_CACHE = values
    return _ENV_CACHE


def env(key: str, default: str = "") -> str:
    """读取环境变量，空值回退到默认值。"""
    value = load_env().get(key)
    return default if value is None or value == "" else value


def env_int(key: str, default: int) -> int:
    """读取整数环境变量；非法值抛出 DeployError。"""
    raw = env(key, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise DeployError(f"环境变量 {key} 必须是整数，当前值为 {raw!r}") from exc


def _validate_config() -> None:
    """校验必需配置、端口范围和项目路径。"""
    missing = [key for key in REQUIRED_ENV if not env(key)]
    if missing:
        raise DeployError(
            "缺少环境变量："
            + ", ".join(missing)
            + "\n请根据 .env.example 补全 .env，或运行 python deploy.py env --force"
        )

    ports: dict[str, int] = {}
    for key, default in DEFAULT_PORTS.items():
        port = env_int(key, default)
        if not 1 <= port <= 65535:
            raise DeployError(f"环境变量 {key} 的端口超出范围：{port}")
        ports[key] = port

    seen: dict[int, str] = {}
    for key, port in ports.items():
        other = seen.get(port)
        if other is not None:
            raise DeployError(f"端口冲突：{other} 与 {key} 都配置为 {port}")
        seen[port] = key

    required_paths = (
        ENV_EXAMPLE,
        COMPOSE_FILE,
        ECOMMERCE_DIR / "Dockerfile",
        CUSTOMER_DIR / "pyproject.toml",
        USER_FRONTEND_DIR / "package.json",
        ADMIN_FRONTEND_DIR / "package.json",
    )
    missing_paths = [str(path) for path in required_paths if not path.exists()]
    if missing_paths:
        raise DeployError("缺少项目文件：\n  " + "\n  ".join(missing_paths))


def _prepare() -> None:
    """确保 .env 存在并完成配置校验。"""
    if not ENV_FILE.exists():
        create_env()
    load_env(refresh=True)
    _validate_config()


def create_env(force: bool = False) -> bool:
    """
    通过 .env.example 创建 .env 文件
    """

    if ENV_FILE.exists() and not force:
        _info(f"{_rel(ENV_FILE)} 已存在，跳过创建（如需覆盖使用 --force）")
        return False
    if not ENV_EXAMPLE.exists():
        raise DeployError(f"找不到模板文件：{_rel(ENV_EXAMPLE)}")

    shutil.copyfile(ENV_EXAMPLE, ENV_FILE)
    os.chmod(ENV_FILE, 0o600)
    load_env(refresh=True)
    _ok(f"已从 {_rel(ENV_EXAMPLE)} 创建 {_rel(ENV_FILE)}")
    return True


# ---------------------------------------------------------------------------
# 命令、进程与 HTTP 工具
# ---------------------------------------------------------------------------


def _run(
    command: Sequence[str | Path],
    *,
    cwd: Path | None = None,
    env_values: dict[str, str] | None = None,
    capture: bool = False,
    check: bool = True,
    announce: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """执行命令并统一处理超时、命令缺失和失败。"""
    args = [str(item) for item in command]
    if announce:
        _info(f"$ {shlex.join(args)}")

    try:
        completed = subprocess.run(
            args,
            cwd=str(cwd) if cwd is not None else None,
            env=env_values,
            capture_output=capture,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise DeployError(f"命令不存在：{args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DeployError(f"命令执行超时：{shlex.join(args)}") from exc
    except OSError as exc:
        raise DeployError(f"无法执行命令 {args[0]}：{exc}") from exc

    if check and completed.returncode != 0:
        if capture:
            output = "\n".join(
                part.strip() for part in (completed.stdout, completed.stderr) if part
            )
            if output:
                _error(output)
        raise DeployError(
            f"命令执行失败（退出码 {completed.returncode}）：{shlex.join(args)}"
        )
    return completed


def _require_command(command: str, hint: str) -> None:
    """检查命令是否在 PATH 中。"""
    if shutil.which(command) is None:
        raise DeployError(f"未找到命令 {command!r}。{hint}")


def _require_docker() -> None:
    """检查 Docker CLI、Compose 插件和 daemon。"""
    global _DOCKER_READY

    _require_command("docker", "请安装 Docker Desktop 并启动。")
    if _DOCKER_READY:
        return

    compose = _run(
        ["docker", "compose", "version"],
        capture=True,
        check=False,
        announce=False,
        timeout=20,
    )
    if compose.returncode != 0:
        raise DeployError("当前 Docker 未安装 Compose 插件，需要 docker compose v2。")

    info = _run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture=True,
        check=False,
        announce=False,
        timeout=20,
    )
    if info.returncode != 0:
        raise DeployError("Docker 守护进程不可用，请先启动 Docker Desktop。")
    _DOCKER_READY = True


def _compose(
    *args: str,
    capture: bool = False,
    check: bool = True,
    announce: bool = True,
) -> subprocess.CompletedProcess[str]:
    """在 ecommerce-service 目录执行 docker compose 命令。"""
    return _run(
        [
            "docker",
            "compose",
            "--env-file",
            str(ENV_FILE),
            "-f",
            str(COMPOSE_FILE),
            *args,
        ],
        cwd=ECOMMERCE_DIR,
        capture=capture,
        check=check,
        announce=announce,
    )


def _compose_service_running(service: str) -> bool:
    """判断 Compose 服务是否有运行中的容器。"""
    result = _compose("ps", "-q", service, capture=True, check=False, announce=False)
    return result.returncode == 0 and bool(result.stdout.strip())


def _compose_image_exists(service: str) -> bool:
    """判断 Compose 服务镜像是否已存在。"""
    result = _compose(
        "images", "-q", service, capture=True, check=False, announce=False
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _probe_host(host: str) -> str:
    """把 0.0.0.0 等监听地址转换成可探测地址。"""
    if host in {"", "*", "0.0.0.0", "::", "[::]"}:
        return "127.0.0.1"
    return host


def _url_host(host: str) -> str:
    """把 IPv6 地址格式化成 URL host。"""
    host = _probe_host(host)
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _http_url(host: str, port: int, path: str = "") -> str:
    """拼接 HTTP 探测 URL。"""
    if path and not path.startswith("/"):
        path = "/" + path
    return f"http://{_url_host(host)}:{port}{path}"


def _display_url(host: str, port: int, path: str = "") -> str:
    """拼接用于展示的 HTTP URL。"""
    return _http_url(host, port, path)


def _http_ok(url: str, timeout: float = 2.0) -> bool:
    """发起 HTTP GET，2xx/3xx 返回 True。"""
    request = urllib.request.Request(
        url, headers={"User-Agent": "shopagent-deploy/1.0"}
    )
    try:
        with _HTTP_OPENER.open(request, timeout=timeout) as response:
            return 200 <= getattr(response, "status", 0) < 400
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _tcp_ok(host: str, port: int) -> bool:
    """尝试 TCP 连接，判断端口是否可访问。"""
    try:
        with socket.create_connection((_probe_host(host), port), timeout=1.0):
            return True
    except OSError:
        return False


def _wait_http(
    name: str,
    url: str,
    *,
    timeout: float = 90.0,
    process: subprocess.Popen | None = None,
    log_path: Path | None = None,
) -> None:
    """轮询 HTTP 直到就绪，进程退出或超时则报错。"""
    _info(f"等待 {name} 就绪：{url}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            _tail_file(log_path)
            raise DeployError(f"{name} 进程已退出（退出码 {process.returncode}）。")
        if _http_ok(url):
            _ok(f"{name} 已就绪")
            return
        time.sleep(0.5)

    _tail_file(log_path)
    raise DeployError(f"{name} 在 {timeout:.0f} 秒内未就绪：{url}")


def _wait_postgres_container(timeout: float = 90.0) -> None:
    """轮询 pg_isready 直到 PostgreSQL 容器就绪。"""
    user = env("POSTGRES_USER", "customer_service")
    _info("等待 PostgreSQL 容器就绪 ...")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = _compose(
            "exec",
            "-T",
            "postgres",
            "pg_isready",
            "-U",
            user,
            capture=True,
            check=False,
            announce=False,
        )
        if result.returncode == 0:
            _ok("PostgreSQL 已就绪")
            return
        time.sleep(1.0)
    raise DeployError(f"PostgreSQL 在 {timeout:.0f} 秒内未就绪。")


def _tail_file(path: Path | None, lines: int = 40) -> None:
    """输出日志文件最后若干行。"""
    if path is None or not path.exists():
        return
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    tail = content[-lines:]
    if tail:
        _warn(f"{_rel(path)} 最后 {len(tail)} 行：")
        print("\n".join(tail))


def _pid_file(name: str) -> Path:
    """返回 PID 文件路径。"""
    return RUN_DIR / f"{name}.pid"


def _read_pid(name: str) -> int | None:
    """读取 PID 文件，不存在或非法时返回 None。"""
    path = _pid_file(name)
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return int(raw)
    except (OSError, ValueError):
        return None


def _write_pid(name: str, pid: int) -> None:
    """写入 PID 文件。"""
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    _pid_file(name).write_text(f"{pid}\n", encoding="utf-8")


def _remove_pid(name: str) -> None:
    """删除 PID 文件。"""
    try:
        _pid_file(name).unlink()
    except FileNotFoundError:
        pass


def _pid_alive(pid: int) -> bool:
    """判断 PID 是否仍存活。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_command(pid: int) -> str:
    """读取进程命令行；不可用时返回空字符串。"""
    if sys.platform == "win32":
        return ""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    return result.stdout.strip()


def _tracked_pid(name: str, marker: str | None = None) -> int | None:
    """读取 PID 并校验进程标记，清理失效 PID 文件。"""
    pid = _read_pid(name)
    if pid is None:
        return None
    if not _pid_alive(pid):
        _remove_pid(name)
        return None

    command = _pid_command(pid)
    if marker and command and marker not in command:
        _warn(f"PID 文件 {_pid_file(name).name} 指向的进程 {pid} 与预期不符，已忽略")
        _remove_pid(name)
        return None
    return pid


def _signal_process(pid: int, sig: int) -> None:
    """向进程或其进程组发送信号。"""
    try:
        if hasattr(os, "getpgid") and os.getpgid(pid) == pid:
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        raise DeployError(f"没有权限停止进程 {pid}。") from exc


def _pid_gone(pid: int, timeout: float) -> bool:
    """等待进程退出并返回是否已退出。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.2)
    return not _pid_alive(pid)


def _stop_process(name: str, marker: str | None = None) -> bool:
    """停止 PID 记录的后台进程，必要时 SIGKILL。"""
    pid = _tracked_pid(name, marker)
    if pid is None:
        _remove_pid(name)
        return False

    _info(f"停止 {name}（PID {pid}）")
    _signal_process(pid, signal.SIGTERM)
    if not _pid_gone(pid, 10.0):
        _warn(f"{name} 未在 10 秒内退出，发送 SIGKILL")
        _signal_process(pid, signal.SIGKILL)
        _pid_gone(pid, 3.0)
    _remove_pid(name)
    _ok(f"{name} 已停止")
    return True


def _ensure_port_free(name: str, host: str, port: int) -> None:
    """确认端口空闲，否则抛出 DeployError。"""
    if _tcp_ok(host, port):
        raise DeployError(
            f"{name} 端口 {port} 已被占用，请先停止占用该端口的进程"
            "（例如手动启动的服务或 npm run dev）。"
        )


def _start_background(
    name: str, command: Sequence[str | Path], cwd: Path
) -> tuple[subprocess.Popen, Path]:
    """后台启动进程并写入 PID 与日志。"""
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    DEPLOY_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = DEPLOY_LOG_DIR / f"{name}.log"

    process_env = os.environ.copy()
    process_env.update(load_env())
    process_env["PYTHONUNBUFFERED"] = "1"

    with log_path.open("ab") as log_handle:
        header = f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n"
        log_handle.write(header.encode("utf-8"))
        log_handle.flush()
        try:
            process = subprocess.Popen(
                [str(item) for item in command],
                cwd=str(cwd),
                env=process_env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            raise DeployError(f"启动 {name} 失败：{exc}") from exc

    _write_pid(name, process.pid)
    _info(f"{name} 已启动（PID {process.pid}），日志：{_rel(log_path)}")
    return process, log_path


# ---------------------------------------------------------------------------
# 构建
# ---------------------------------------------------------------------------


def build_ecommerce_service() -> bool:
    """
    构建电商服务
    """

    _prepare()
    _require_docker()
    _section("构建 ecommerce-service")
    _compose("build", "app")
    _ok("ecommerce-service 镜像构建完成")
    return True


def _customer_venv_python() -> Path | None:
    """返回 customer-service 虚拟环境 Python 路径。"""
    candidates = (
        CUSTOMER_DIR / ".venv" / "bin" / "python",
        CUSTOMER_DIR / ".venv" / "Scripts" / "python.exe",
    )
    return next((path for path in candidates if path.exists()), None)


def _customer_uvicorn() -> Path | None:
    """返回 customer-service 虚拟环境 uvicorn 路径。"""
    candidates = (
        CUSTOMER_DIR / ".venv" / "bin" / "uvicorn",
        CUSTOMER_DIR / ".venv" / "Scripts" / "uvicorn.exe",
    )
    return next((path for path in candidates if path.exists()), None)


def _ensure_customer_build() -> None:
    """虚拟环境缺失时构建 customer-service。"""
    if _customer_venv_python() is None or _customer_uvicorn() is None:
        build_customer_service()


def build_customer_service() -> bool:
    """
    构建客户服务
    """

    _prepare()
    _require_command("uv", "请先安装 uv：https://docs.astral.sh/uv/")
    _section("构建 customer-service")
    _run(["uv", "sync", "--frozen"], cwd=CUSTOMER_DIR)
    if _customer_venv_python() is None or _customer_uvicorn() is None:
        raise DeployError("customer-service 虚拟环境创建失败，请检查 uv sync 输出。")
    _ok("customer-service 依赖安装完成")
    return True


def _ai_service_has_code() -> bool:
    """判断 ai-service 目录是否已有实现文件。"""
    if not AI_DIR.exists():
        return False
    ignored = {".DS_Store", "__pycache__", ".gitkeep"}
    return any(path.name not in ignored for path in AI_DIR.iterdir())


def build_ai_service() -> bool:
    """
    构建 AI 服务
    """

    _prepare()
    if not _ai_service_has_code():
        _warn("ai-service 目录为空（尚未实现），跳过构建")
        return False
    raise DeployError(
        "ai-service 目录中出现文件，但 deploy.py 尚未实现其构建逻辑，请先补充脚本。"
    )


def _vite_binary(directory: Path) -> Path | None:
    """返回前端 node_modules 中的 Vite 可执行文件。"""
    bin_dir = directory / "node_modules" / ".bin"
    for name in ("vite", "vite.cmd"):
        candidate = bin_dir / name
        if candidate.exists():
            return candidate
    return None


def _build_frontend(frontend: _Frontend) -> None:
    """安装依赖并构建单个前端。"""
    _section(f"构建 {frontend.name}")
    if not frontend.directory.exists():
        raise DeployError(f"前端目录不存在：{_rel(frontend.directory)}")

    if (frontend.directory / "node_modules").exists():
        install_command = ["npm", "install", "--no-audit", "--no-fund"]
    elif (frontend.directory / "package-lock.json").exists():
        install_command = ["npm", "ci", "--no-audit", "--no-fund"]
    else:
        install_command = ["npm", "install", "--no-audit", "--no-fund"]

    _run(install_command, cwd=frontend.directory)
    _run(["npm", "run", "build"], cwd=frontend.directory)

    if not (frontend.directory / "dist" / "index.html").exists():
        raise DeployError(f"{frontend.name} 构建产物缺少 dist/index.html")
    _ok(f"{frontend.name} 构建完成")


def build_frontend() -> bool:
    """
    构建前端
    """

    _prepare()
    _require_command("node", "请安装 Node.js 18 或更高版本。")
    _require_command("npm", "请安装 npm。")
    for frontend in _FRONTENDS:
        _build_frontend(frontend)
    return True


def build_all_services() -> bool:
    """检查环境后构建全部服务。"""
    ensure_environment()
    build_ecommerce_service()
    build_customer_service()
    build_ai_service()
    build_frontend()
    return True


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------


def _ensure_database_infra() -> None:
    """确保 PostgreSQL/Redis 可用，必要时启动容器。"""
    host = env("POSTGRES_HOST", "127.0.0.1")
    port = env_int("POSTGRES_PORT", 18200)
    if _tcp_ok(host, port):
        _info(f"PostgreSQL 已运行：{host}:{port}")
        return

    _require_docker()
    _info("启动 PostgreSQL / Redis 容器 ...")
    _compose("up", "-d", "postgres", "redis")
    _wait_postgres_container()


def _ensure_customer_schema() -> None:
    """幂等创建 customer-service 数据表。"""
    python = _customer_venv_python()
    if python is None:
        raise DeployError("customer-service 虚拟环境不存在，请先执行构建。")
    _info("同步 customer-service 数据表结构 ...")
    try:
        _run(
            [python, "-c", CREATE_CUSTOMER_TABLES_CODE],
            cwd=CUSTOMER_DIR,
            capture=True,
            announce=False,
        )
    except DeployError as exc:
        raise DeployError(
            "customer-service 数据表初始化失败，请确认数据库可用。"
        ) from exc


def _reset_ai_database() -> None:
    """重置 ai_customer_service schema 并保留 pgvector。"""
    user = env("POSTGRES_USER", "customer_service")
    sql = (
        "DROP SCHEMA IF EXISTS public CASCADE; "
        "CREATE SCHEMA public; "
        "CREATE EXTENSION IF NOT EXISTS vector;"
    )
    _info("重置 ai_customer_service ...")
    result = _compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        user,
        "-d",
        "ai_customer_service",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        sql,
        capture=True,
        check=False,
        announce=False,
    )
    if result.returncode != 0:
        output = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part
        )
        if output:
            _error(output)
        raise DeployError("重置 ai_customer_service 失败。")


def reset_all_databases(assume_yes: bool = False) -> bool:
    """
    重置数据库
    """

    _prepare()
    _require_docker()
    _require_command("uv", "请先安装 uv：https://docs.astral.sh/uv/")

    _section("重置全部数据库")
    _warn(
        "该操作会删除 PostgreSQL 数据卷（customer_service / ecommerce / "
        "ai_customer_service）和 Redis 数据，且不可恢复。"
    )
    if not _confirm("确认继续吗？", assume_yes):
        _info("已取消数据库重置")
        return False

    ecommerce_was_running = _compose_service_running("app")
    customer_was_running = (
        _tracked_pid("customer-service", "customer_service.app.app") is not None
    )
    if customer_was_running:
        _stop_process("customer-service", "customer_service.app.app")

    _info("删除数据卷并重建基础设施 ...")
    _compose("down", "-v", "--remove-orphans")
    _compose("up", "-d", "postgres", "redis")
    _wait_postgres_container()

    if not _compose_image_exists("app"):
        build_ecommerce_service()
    _info("重建 ecommerce 数据库并写入演示数据 ...")
    _compose(
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "app",
        "python",
        "-m",
        "ecommerce_service.reset_data",
    )

    _ensure_customer_build()
    _info("重建 customer-service 数据库 ...")
    _run(
        [
            _customer_venv_python(),
            "-m",
            "customer_service.scripts.init_db",
            "-y",
        ],
        cwd=CUSTOMER_DIR,
    )

    _reset_ai_database()
    _compose(
        "exec",
        "-T",
        "redis",
        "redis-cli",
        "FLUSHALL",
        capture=True,
        check=False,
        announce=False,
    )
    _ok("全部数据库已重置")

    if ecommerce_was_running:
        _info("恢复重置前正在运行的 ecommerce-service ...")
        deploy_ecommerce_service(rebuild=False)
    if customer_was_running:
        _info("恢复重置前正在运行的 customer-service ...")
        deploy_customer_service()
    return True


# ---------------------------------------------------------------------------
# 部署
# ---------------------------------------------------------------------------


def _show_compose_logs(service: str, lines: int = 60) -> None:
    """输出 Compose 服务最近日志。"""
    result = _compose(
        "logs",
        "--tail",
        str(lines),
        service,
        capture=True,
        check=False,
        announce=False,
    )
    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part)
    if output:
        _warn(f"{service} 最后 {lines} 行日志：")
        print(output)


def deploy_ecommerce_service(rebuild: bool = True) -> bool:
    """
    部署电商服务
    """

    _prepare()
    _require_docker()
    _section("部署 ecommerce-service")

    args = ["up", "-d"]
    if rebuild:
        args.append("--build")
    _compose(*args)

    host = env("ECOMMERCE_SERVICE_HOST", "127.0.0.1")
    port = env_int("ECOMMERCE_SERVICE_PORT", 18001)
    try:
        _wait_http(
            "ecommerce-service",
            _http_url(host, port, "/health"),
            timeout=180.0,
        )
    except DeployError:
        _show_compose_logs("app")
        raise

    _ok(f"ecommerce-service：{_display_url(host, port)}")
    return True


def stop_ecommerce_service() -> bool:
    """停止 ecommerce-service 的 app 容器，保留 PostgreSQL 和 Redis。"""

    _prepare()
    _require_docker()
    _section("停止 ecommerce-service")
    _compose("stop", "app")
    _ok("ecommerce-service app 容器已停止")
    return True


def deploy_customer_service() -> bool:
    """
    部署客户服务
    """

    _prepare()
    _ensure_customer_build()
    _ensure_database_infra()
    _ensure_customer_schema()

    host = env("CUSTOMER_SERVICE_HOST", "127.0.0.1")
    port = env_int("CUSTOMER_SERVICE_PORT", 18000)
    uvicorn = _customer_uvicorn()
    if uvicorn is None:
        raise DeployError("customer-service 虚拟环境中找不到 uvicorn。")

    _section("部署 customer-service")
    _stop_process("customer-service", "customer_service.app.app")
    _ensure_port_free("customer-service", host, port)

    process, log_path = _start_background(
        "customer-service",
        [
            uvicorn,
            "customer_service.app.app:app",
            "--host",
            host,
            "--port",
            str(port),
        ],
        CUSTOMER_DIR,
    )
    try:
        _wait_http(
            "customer-service",
            _http_url(host, port, "/openapi.json"),
            timeout=90.0,
            process=process,
            log_path=log_path,
        )
    except DeployError:
        _stop_process("customer-service", "customer_service.app.app")
        raise

    _ok(f"customer-service：{_display_url(host, port)}")
    _ok(f"接口文档：{_display_url(host, port, '/docs')}")
    return True


def deploy_ai_service() -> bool:
    """
    部署 AI 服务
    """

    _prepare()
    if not _ai_service_has_code():
        _warn("ai-service 目录为空（尚未实现），跳过部署")
        return False
    raise DeployError(
        "ai-service 目录中出现文件，但 deploy.py 尚未实现其部署逻辑，请先补充脚本。"
    )


def _frontend_host(frontend: _Frontend) -> str:
    """读取前端 HOST 配置。"""
    return env(f"{frontend.env_prefix}_HOST", "127.0.0.1")


def _frontend_port(frontend: _Frontend) -> int:
    """读取前端 PORT 配置。"""
    return env_int(f"{frontend.env_prefix}_PORT", frontend.default_port)


def _deploy_frontend(frontend: _Frontend) -> None:
    """构建产物存在时用 Vite preview 启动单个前端。"""
    dist_index = frontend.directory / "dist" / "index.html"
    if not dist_index.exists():
        raise DeployError(
            f"{frontend.name} 尚未构建，请先运行 python deploy.py build frontend"
        )

    vite = _vite_binary(frontend.directory)
    if vite is None:
        raise DeployError(
            f"{frontend.name} 缺少 Vite 依赖，请先运行 python deploy.py build frontend"
        )

    host = _frontend_host(frontend)
    port = _frontend_port(frontend)

    _section(f"部署 {frontend.name}")
    _stop_process(frontend.name, "vite")
    _ensure_port_free(frontend.name, host, port)

    process, log_path = _start_background(
        frontend.name,
        [
            vite,
            "preview",
            "--host",
            host,
            "--port",
            str(port),
            "--strictPort",
        ],
        frontend.directory,
    )
    try:
        _wait_http(
            frontend.name,
            _http_url(host, port, "/"),
            timeout=60.0,
            process=process,
            log_path=log_path,
        )
    except DeployError:
        _stop_process(frontend.name, "vite")
        raise

    _ok(f"{frontend.name}：{_display_url(host, port)}")


def deploy_frontend() -> bool:
    """
    部署前端
    """

    _prepare()
    for frontend in _FRONTENDS:
        _deploy_frontend(frontend)
    return True


def deploy_all_services(rebuild: bool = True) -> bool:
    """
    部署所有服务
    """

    ensure_environment()
    _section("开始部署全部服务")
    deploy_ecommerce_service(rebuild=rebuild)
    deploy_customer_service()
    deploy_ai_service()
    deploy_frontend()
    check_status()
    return True


def undeploy_all_services(remove_volumes: bool = False) -> bool:
    """
    卸载所有服务
    """

    _prepare()
    _section("停止并卸载全部服务")

    _stop_process("customer-service", "customer_service.app.app")
    for frontend in _FRONTENDS:
        _stop_process(frontend.name, "vite")

    if shutil.which("docker") is not None:
        down_args = ["down", "--remove-orphans"]
        if remove_volumes:
            down_args.append("-v")
        result = _compose(*down_args, check=False)
        if result.returncode == 0:
            _ok("ecommerce-service 容器已停止")
        else:
            _warn("docker compose down 执行失败，请检查 Docker 状态。")
    else:
        _warn("未找到 docker，跳过容器清理。")

    if remove_volumes:
        _warn("PostgreSQL / Redis 数据卷已删除")
    else:
        _info("PostgreSQL / Redis 数据卷已保留，下次部署会继续使用")
    return True


# ---------------------------------------------------------------------------
# 环境检查与状态
# ---------------------------------------------------------------------------


def _check_node_version() -> None:
    """检查 Node.js 主版本不低于 18。"""
    result = _run(
        ["node", "--version"], capture=True, check=False, announce=False, timeout=10
    )
    if result.returncode != 0:
        raise DeployError("无法执行 node --version，请检查 Node.js 安装。")
    raw = result.stdout.strip().lstrip("v")
    try:
        major = int(raw.split(".", 1)[0])
    except ValueError as exc:
        raise DeployError(f"无法识别 Node.js 版本：{raw}") from exc
    if major < 18:
        raise DeployError(f"Node.js 版本过低（{raw}），需要 18 或更高版本。")


def ensure_environment() -> bool:
    """
    检查环境配置
    """

    _section("检查部署环境")
    _prepare()
    _require_docker()
    _require_command("uv", "请先安装 uv：https://docs.astral.sh/uv/")
    _require_command("node", "请安装 Node.js 18 或更高版本。")
    _require_command("npm", "请安装 npm。")
    _check_node_version()

    _ok(f"配置文件：{_rel(ENV_FILE)}")
    _ok("Docker、uv、Node.js、npm 均可用")
    _info("前端访问地址：")
    for frontend in _FRONTENDS:
        print(
            f"         {frontend.name:<16} "
            f"{_display_url(_frontend_host(frontend), _frontend_port(frontend))}"
        )
    return True


def _check_command(label: str, command: Sequence[str]) -> _Check:
    """执行命令并返回可用性检查；示例：["node", "--version"] -> [OK] node v26.13.0。"""

    if shutil.which(command[0]) is None:
        return _Check(label, False, "not found")
    result = _run(command, capture=True, check=False, announce=False, timeout=15)
    if result.returncode != 0:
        return _Check(label, False, "command failed")
    output = (result.stdout or result.stderr).strip().splitlines()
    return _Check(label, True, output[0].strip() if output else "ok")


def _collect_diagnostics() -> _Diagnostics:
    """汇总依赖、配置、端口和服务状态，供 ASCII 控制台渲染。"""

    checks: list[_Check] = []
    env_existed = ENV_FILE.exists()
    configured = True
    try:
        _prepare()
        detail = _rel(ENV_FILE) if env_existed else "created from .env.example"
        checks.append(_Check("config", True, detail))
    except (DeployError, OSError) as exc:
        configured = False
        checks.append(_Check("config", False, str(exc).splitlines()[0]))

    checks.extend(
        (
            _check_command("docker", ["docker", "compose", "version"]),
            _check_command(
                "docker-daemon", ["docker", "info", "--format", "{{.ServerVersion}}"]
            ),
            _check_command("uv", ["uv", "--version"]),
            _check_command("node", ["node", "--version"]),
            _check_command("npm", ["npm", "--version"]),
        )
    )

    services: list[_ServiceInfo] = []
    ports: list[_PortInfo] = []
    if configured:
        services = _probe_services()
        ports = _probe_ports(services)
        conflicts = _port_conflicts(services, ports)
        checks.append(
            _Check(
                "ports",
                not conflicts,
                "; ".join(conflicts) if conflicts else f"{len(ports)} ports checked",
            )
        )
    else:
        checks.append(_Check("ports", False, "skipped: config invalid"))

    return _Diagnostics(
        checks=checks,
        services=services,
        ports=ports,
        configured=configured,
    )


def _probe_services() -> list[_ServiceInfo]:
    """探测各服务的进程/容器与 HTTP 健康状态，返回服务列表。"""

    services: list[_ServiceInfo] = []

    ecommerce_host = env("ECOMMERCE_SERVICE_HOST", "127.0.0.1")
    ecommerce_port = env_int("ECOMMERCE_SERVICE_PORT", 18001)
    ecommerce_url = _display_url(ecommerce_host, ecommerce_port)
    ecommerce_health = _http_ok(_http_url(ecommerce_host, ecommerce_port, "/health"))
    containers = 0
    app_running = False
    if shutil.which("docker") is not None:
        result = _compose("ps", "-q", capture=True, check=False, announce=False)
        if result.returncode == 0:
            containers = len(result.stdout.splitlines())
        app_running = _compose_service_running("app")
    if ecommerce_health:
        ecommerce_state = "running"
        ecommerce_detail = f"{containers} containers"
    elif app_running:
        ecommerce_state = "degraded"
        ecommerce_detail = "app running, health pending"
    else:
        ecommerce_state = "stopped"
        ecommerce_detail = "not running"
    services.append(
        _ServiceInfo(
            "ecommerce",
            "Ecommerce",
            ecommerce_host,
            ecommerce_port,
            ecommerce_url,
            ecommerce_state,
            ecommerce_detail,
        )
    )

    customer_host = env("CUSTOMER_SERVICE_HOST", "127.0.0.1")
    customer_port = env_int("CUSTOMER_SERVICE_PORT", 18000)
    customer_url = _display_url(customer_host, customer_port)
    customer_pid = _tracked_pid("customer-service", "customer_service.app.app")
    customer_health = _http_ok(_http_url(customer_host, customer_port, "/openapi.json"))
    if customer_health and customer_pid is not None:
        customer_state = "running"
        customer_detail = f"pid {customer_pid}"
    elif customer_health:
        customer_state = "external"
        customer_detail = "external process"
    elif customer_pid is not None:
        customer_state = "degraded"
        customer_detail = f"pid {customer_pid}, health pending"
    else:
        customer_state = "stopped"
        customer_detail = "not running"
    services.append(
        _ServiceInfo(
            "customer",
            "Customer",
            customer_host,
            customer_port,
            customer_url,
            customer_state,
            customer_detail,
            customer_pid,
        )
    )

    services.append(
        _ServiceInfo(
            "ai",
            "AI Service",
            env("AI_SERVICE_HOST", "127.0.0.1"),
            env_int("AI_SERVICE_PORT", 18002),
            _display_url(
                env("AI_SERVICE_HOST", "127.0.0.1"),
                env_int("AI_SERVICE_PORT", 18002),
            ),
            "missing",
            "not implemented",
            implemented=False,
        )
    )

    for frontend in _FRONTENDS:
        host = _frontend_host(frontend)
        port = _frontend_port(frontend)
        url = _display_url(host, port)
        pid = _tracked_pid(frontend.name, "vite")
        health = _http_ok(_http_url(host, port, "/"))
        if health and pid is not None:
            state = "running"
            detail = f"pid {pid}"
        elif health:
            state = "external"
            detail = "external process"
        elif pid is not None:
            state = "degraded"
            detail = f"pid {pid}, page pending"
        else:
            state = "stopped"
            detail = "not running"
        services.append(
            _ServiceInfo(
                frontend.name,
                frontend.name.replace("-frontend", "").title() + " Frontend",
                host,
                port,
                url,
                state,
                detail,
                pid,
            )
        )

    return services


def _probe_ports(services: Sequence[_ServiceInfo]) -> list[_PortInfo]:
    """检查服务端口和数据库端口是否被监听。"""

    ports = [
        _PortInfo(service.label, service.port, _tcp_ok(service.host, service.port))
        for service in services
    ]

    postgres_host = env("POSTGRES_HOST", "127.0.0.1")
    postgres_port = env_int("POSTGRES_PORT", 18200)
    ports.append(
        _PortInfo(
            "postgres",
            postgres_port,
            _tcp_ok(postgres_host, postgres_port),
        )
    )

    redis_port = env_int("REDIS_PORT", 18201)
    ports.append(_PortInfo("redis", redis_port, _tcp_ok("127.0.0.1", redis_port)))
    return ports


def _port_conflicts(
    services: Sequence[_ServiceInfo], ports: Sequence[_PortInfo]
) -> list[str]:
    """返回端口占用与服务状态不一致的冲突说明。"""

    by_label = {port.label: port for port in ports}
    conflicts: list[str] = []
    for service in services:
        if not service.implemented:
            continue
        port = by_label.get(service.label)
        if port is None:
            continue
        if (service.running or service.state == "degraded") and not port.in_use:
            conflicts.append(f"{service.label} port {port.port} not listening")
        elif service.state == "stopped" and port.in_use:
            conflicts.append(f"{service.label} port {port.port} occupied")
    return conflicts


def _status_symbol(ok: bool) -> str:
    """返回命令行状态符号。"""

    if ok:
        return _paint("✓", "32")
    return _paint("✗", "31")


def _status_line(label: str, ok: bool, detail: str) -> None:
    """输出一行命令行服务状态。"""

    print(f"  {_status_symbol(ok)} {label:<22} {detail}")


def check_status() -> bool:
    """
    检查服务状态
    """

    _section("服务状态")
    _prepare()
    services = _probe_services()

    for service in services:
        if not service.implemented:
            print(f"  {_paint('–', '90')} {service.label:<22} {service.detail}")
            continue
        _status_line(
            service.label,
            service.running,
            f"{service.url}（{service.detail}）",
        )

    print()
    print(f"  日志目录：{_rel(DEPLOY_LOG_DIR)}")
    return all(service.running for service in services if service.implemented)


# ---------------------------------------------------------------------------
# 交互式控制台与命令行入口
# ---------------------------------------------------------------------------


def _confirm(question: str, assume_yes: bool = False) -> bool:
    """交互式 y/N 确认；非 TTY 且未加 --yes 时报错。"""
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise DeployError("当前是非交互环境，请使用 --yes 跳过确认。")
    try:
        answer = input(f"{question} [y/N]: ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def _normalize_components(values: Sequence[str]) -> list[str]:
    """规范化服务名。

    示例：['customer-service'] -> ['customer']；['all'] -> ['ecommerce', 'customer', 'ai', 'frontend']。
    """
    if not values:
        return list(ALL_COMPONENTS)

    selected: set[str] = set()
    for raw in values:
        value = raw.strip().lower()
        if value in {"", "all"}:
            return list(ALL_COMPONENTS)
        value = COMPONENT_ALIASES.get(value, value)
        if value not in ALL_COMPONENTS:
            raise DeployError(
                f"未知服务 {raw!r}，可选值：{', '.join(ALL_COMPONENTS)}, all"
            )
        selected.add(value)
    return [component for component in ALL_COMPONENTS if component in selected]


def _build_selected(values: Sequence[str]) -> bool:
    """按命令参数构建选中的组件。"""
    components = _normalize_components(values)
    if components == list(ALL_COMPONENTS):
        return build_all_services()

    _prepare()
    for component in components:
        if component == "ecommerce":
            build_ecommerce_service()
        elif component == "customer":
            build_customer_service()
        elif component == "ai":
            build_ai_service()
        elif component == "frontend":
            build_frontend()
    return True


def _deploy_selected(values: Sequence[str], rebuild: bool = True) -> bool:
    """按命令参数部署选中的组件。"""
    components = _normalize_components(values)
    if components == list(ALL_COMPONENTS):
        return deploy_all_services(rebuild=rebuild)

    _prepare()
    for component in components:
        if component == "ecommerce":
            deploy_ecommerce_service(rebuild=rebuild)
        elif component == "customer":
            deploy_customer_service()
        elif component == "ai":
            deploy_ai_service()
        elif component == "frontend":
            deploy_frontend()
    check_status()
    return True


# ---------------------------------------------------------------------------
# ASCII 控制台
# ---------------------------------------------------------------------------


def _strip_ansi(text: str) -> str:
    """删除 ANSI 控制序列，便于计算显示宽度。"""

    return _ANSI_RE.sub("", text)


def _display_width(text: str) -> int:
    """返回文本在终端中的显示宽度（中文按 2 列计算）。"""

    width = 0
    for char in _strip_ansi(text):
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return width


def _fit_text(text: str, width: int) -> str:
    """按终端显示宽度截断并用空格补齐文本。"""

    if width <= 0:
        return ""
    plain = _strip_ansi(text)
    truncate = _display_width(plain) > width
    limit = width - 1 if truncate else width
    result: list[str] = []
    used = 0
    for char in plain:
        char_width = 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if used + char_width > limit:
            break
        result.append(char)
        used += char_width
    if truncate:
        result.append("~")
        used += 1
    return "".join(result) + " " * max(0, width - used)


def _box_border(width: int) -> str:
    """生成 ASCII 边框线。"""

    return "+" + "-" * max(0, width - 2) + "+"


def _box_line(text: str, width: int) -> str:
    """生成一行带左右边框的 ASCII 内容。"""

    return "| " + _fit_text(text, max(0, width - 4)) + " |"


def _terminal_size() -> tuple[int, int]:
    """读取终端宽高，返回至少 40x12 的可用尺寸。"""

    size = shutil.get_terminal_size(fallback=(100, 30))
    return max(40, size.columns), max(12, size.lines)


def _entry_keys(state: _Diagnostics) -> list[str]:
    """返回控制台全部可选行的键：服务和全局操作。"""

    keys = [f"svc:{service.key}" for service in state.services]
    keys.extend(f"act:{action.key}" for action in _ACTIONS)
    return keys


def _find_service(state: _Diagnostics, key: str) -> _ServiceInfo | None:
    """按控制台键查找服务。"""

    return next(
        (service for service in state.services if f"svc:{service.key}" == key), None
    )


def _find_action(key: str) -> _ActionInfo | None:
    """按控制台键查找全局操作。"""

    return next((action for action in _ACTIONS if f"act:{action.key}" == key), None)


def _first_service_key(state: _Diagnostics) -> str:
    """返回第一个服务键；没有服务时返回空字符串。"""

    return f"svc:{state.services[0].key}" if state.services else ""


def _first_action_key() -> str:
    """返回第一个全局操作键。"""

    return f"act:{_ACTIONS[0].key}"


def _move_selection(state: _Diagnostics, selected: str, delta: int) -> str:
    """按上下方向移动选择，示例：services[0] + 1 -> services[1]。"""

    keys = _entry_keys(state)
    if not keys:
        return ""
    try:
        index = keys.index(selected)
    except ValueError:
        index = 0
    return keys[(index + delta) % len(keys)]


def _service_token(service: _ServiceInfo) -> str:
    """返回服务的 ASCII 状态标记。"""

    if service.state in {"running", "external"}:
        return "[OK]"
    if service.state == "degraded":
        return "[!!]"
    return "[--]"


def _check_token(check: _Check) -> str:
    """返回检查项的 ASCII 状态标记。"""

    return f"[OK] {check.label}" if check.ok else f"[!!] {check.label}"


def _pack_tokens(prefix: str, tokens: Sequence[str], width: int) -> list[str]:
    """把短标记按显示宽度打包成多行。"""

    if not tokens:
        return [prefix.rstrip()]
    lines: list[str] = []
    current = prefix
    has_item = False
    for token in tokens:
        addition = ("  " if has_item else "") + token
        if has_item and _display_width(current) + _display_width(addition) > width:
            lines.append(current)
            current = " " * _display_width(prefix) + token
        else:
            current += addition
        has_item = True
    if has_item:
        lines.append(current)
    return lines


def _console_rows(
    state: _Diagnostics, width: int, selected_key: str
) -> list[_ConsoleRow]:
    """把诊断数据转换成控制台内容行。"""

    rows: list[_ConsoleRow] = []
    rows.append(_ConsoleRow("CHECKS"))
    rows.extend(
        _ConsoleRow(line)
        for line in _pack_tokens(
            "  ", [_check_token(check) for check in state.checks], width
        )
    )
    failures = [
        f"{check.label}: {check.detail}"
        for check in state.checks
        if not check.ok and check.detail
    ]
    if failures:
        rows.extend(_ConsoleRow(line) for line in _pack_tokens("  ! ", failures, width))
    versions = [
        f"{check.label}={check.detail}"
        for check in state.checks
        if check.ok and check.detail and check.label in {"docker", "uv", "node", "npm"}
    ]
    if versions:
        rows.extend(
            _ConsoleRow(line) for line in _pack_tokens("  ver ", versions, width)
        )

    rows.append(_ConsoleRow(""))
    rows.append(_ConsoleRow("SERVICES"))
    for service in state.services:
        key = f"svc:{service.key}"
        marker = ">" if key == selected_key else " "
        text = (
            f"{marker} {_service_token(service)} {service.label:<18} "
            f"{service.state.upper():<9} {service.url}  {service.detail}"
        )
        rows.append(_ConsoleRow(text, key))

    if state.ports:
        rows.append(_ConsoleRow(""))
        rows.append(_ConsoleRow("PORTS"))
        port_tokens = [
            f"{port.label}:{port.port} {'USED' if port.in_use else 'FREE'}"
            for port in state.ports
        ]
        rows.extend(
            _ConsoleRow(line) for line in _pack_tokens("  ", port_tokens, width)
        )

    rows.append(_ConsoleRow(""))
    rows.append(_ConsoleRow("ACTIONS"))
    for action in _ACTIONS:
        key = f"act:{action.key}"
        marker = ">" if key == selected_key else " "
        rows.append(_ConsoleRow(f"{marker} {action.label:<18} {action.detail}", key))

    if not state.configured:
        rows.append(_ConsoleRow(""))
        rows.append(
            _ConsoleRow(
                "Configuration is invalid; fix .env and press F to run checks again."
            )
        )
    return rows


def _selection_message(state: _Diagnostics, selected_key: str) -> str:
    """返回当前选择项的一行摘要。"""

    service = _find_service(state, selected_key)
    if service is not None:
        return f"Selected {service.label}: {service.state}."
    action = _find_action(selected_key)
    if action is not None:
        return action.detail
    return "Select a service or action."


def _footer_hints(state: _Diagnostics, selected_key: str) -> list[str]:
    """根据当前选择返回底部快捷键说明。"""

    service = _find_service(state, selected_key)
    action = _find_action(selected_key)
    if service is not None:
        if not service.implemented:
            return [
                "AI not implemented  F=refresh  ?=help  Q/Esc=quit",
                "Arrows=select  J/K=page  A=all  D=reset",
            ]
        return [
            "Enter=start/restart  B=build  R=restart  S=stop  L=logs  F=refresh",
            "A=all  D=reset  Arrows=select  J/K=page  ?=help  Q/Esc=quit",
        ]
    if action is not None:
        return [
            "Enter=run  F=refresh  Arrows=select  J/K=page  ?=help  Q/Esc=quit",
            "A=all  D=reset",
        ]
    return ["F=refresh  ?=help  Q/Esc=quit", ""]


def _console_content_height(height: int) -> int:
    """返回控制台内容区高度。"""

    return max(1, height - 8)


def _render_console_lines(
    state: _Diagnostics,
    selected_key: str,
    scroll: int,
    width: int,
    height: int,
    message: str,
    hint_override: Sequence[str] | None = None,
) -> list[str]:
    """渲染 ASCII 控制台，返回恰好终端高度的文本行。"""

    inner = max(1, width - 4)
    content_height = _console_content_height(height)
    rows = _console_rows(state, inner, selected_key)
    total = len(rows)
    max_scroll = max(0, total - content_height)
    scroll = max(0, min(scroll, max_scroll))
    start = scroll
    end = min(total, start + content_height)
    visible = list(rows[start:end])
    while len(visible) < content_height:
        visible.append(_ConsoleRow(""))

    lines = [_box_border(width)]
    left = "SHOPAGENT DEPLOY CONSOLE"
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    position = f"rows {start + 1}-{end}/{total}" if total else "rows 0/0"
    right = f"{stamp}  {position}"
    gap = inner - _display_width(left) - _display_width(right)
    title = left + " " * max(1, gap) + right if gap >= 1 else left
    lines.append("\033[1m" + _box_line(title, width) + "\033[0m")
    lines.append(_box_border(width))

    for row in visible:
        line = _box_line(row.text, width)
        if row.text in {"CHECKS", "SERVICES", "PORTS", "ACTIONS"}:
            line = "\033[1m" + line + "\033[0m"
        if row.key and row.key == selected_key:
            line = "\033[7m" + line + "\033[0m"
        lines.append(line)

    lines.append(_box_border(width))
    lines.append(_box_line(f"MSG  {message}", width))
    hints = (
        list(hint_override)
        if hint_override is not None
        else _footer_hints(state, selected_key)
    )
    while len(hints) < 2:
        hints.append("")
    for hint in hints[:2]:
        lines.append(_box_line(f"KEYS {hint}", width))
    lines.append(_box_border(width))
    return lines[:height]


def _render_viewer_lines(
    title: str,
    lines_in: Sequence[str],
    offset: int,
    width: int,
    height: int,
    footer: str,
) -> list[str]:
    """渲染帮助或日志查看器。"""

    content_height = max(1, height - 6)
    total = len(lines_in)
    max_offset = max(0, total - content_height)
    offset = max(0, min(offset, max_offset))
    end = min(total, offset + content_height)
    visible = list(lines_in[offset:end])
    while len(visible) < content_height:
        visible.append("")

    lines = [_box_border(width)]
    lines.append("\033[1m" + _box_line(title, width) + "\033[0m")
    lines.append(_box_border(width))
    for line in visible:
        lines.append(_box_line(line, width))
    lines.append(_box_border(width))
    lines.append(_box_line(footer, width))
    lines.append(_box_border(width))
    return lines[:height]


def _draw_lines(lines: Sequence[str]) -> None:
    """把控制台行写到终端并清掉残留内容。"""

    sys.stdout.write("\033[H" + "\n".join(lines) + "\033[J")
    sys.stdout.flush()


def _reveal_selection(
    state: _Diagnostics,
    selected_key: str,
    scroll: int,
    width: int,
    height: int,
) -> int:
    """调整滚动位置，使当前选择行可见。"""

    rows = _console_rows(state, max(1, width - 4), selected_key)
    index = next((i for i, row in enumerate(rows) if row.key == selected_key), None)
    if index is None:
        return 0
    content_height = _console_content_height(height)
    if index < scroll:
        return index
    if index >= scroll + content_height:
        return index - content_height + 1
    return scroll


class _TerminalSession:
    """管理 raw mode 与 alternate screen，并读取按键。"""

    def __init__(self) -> None:
        """记录标准输入文件描述符。"""

        self.fd = sys.stdin.fileno()
        self.old_attrs: list[object] | None = None
        self.active = False

    def _setup(self) -> None:
        """进入 raw mode 和 alternate screen。"""

        if self.active:
            return
        self.old_attrs = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        self.active = True
        sys.stdout.write("\033[?1049h\033[?25l\033[2J\033[H")
        sys.stdout.flush()

    def _restore(self) -> None:
        """恢复终端状态并离开 alternate screen。"""

        if not self.active:
            return
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()
        if self.old_attrs is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old_attrs)
        self.active = False

    def __enter__(self) -> Self:
        """进入终端会话。"""

        self._setup()
        return self

    def __exit__(self, *_: object) -> None:
        """退出终端会话。"""

        self._restore()

    def suspend(self) -> None:
        """临时挂起控制台，执行普通命令。"""

        self._restore()

    def resume(self) -> None:
        """重新进入控制台。"""

        self._setup()

    def _read_byte(self, timeout: float | None) -> bytes:
        """读取一个字节；超时返回空字节串。"""

        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return b""
        return os.read(self.fd, 1)

    def read_key(self) -> str:
        """读取一个按键，返回 up/down/enter/esc 或单个字符。"""

        data = self._read_byte(None)
        if not data:
            return "esc"
        if data == b"\x1b":
            second = self._read_byte(0.02)
            if not second:
                return "esc"
            if second not in {b"[", b"O"}:
                return "unknown"
            third = self._read_byte(0.02)
            if not third:
                return "unknown"
            code = third.decode("latin-1")
            if code in "ABCDHF":
                return {
                    "A": "up",
                    "B": "down",
                    "C": "right",
                    "D": "left",
                    "H": "home",
                    "F": "end",
                }[code]
            if code.isdigit():
                sequence = code
                while len(sequence) < 8:
                    nxt = self._read_byte(0.01)
                    if not nxt:
                        break
                    sequence += nxt.decode("latin-1")
                    if sequence.endswith("~") or sequence[-1] in "ABCDHF":
                        break
                if sequence.endswith("5~"):
                    return "pageup"
                if sequence.endswith("6~"):
                    return "pagedown"
                last = sequence[-1]
                if last in "ABCDHF":
                    return {
                        "A": "up",
                        "B": "down",
                        "C": "right",
                        "D": "left",
                        "H": "home",
                        "F": "end",
                    }[last]
            return "unknown"

        char = data.decode("utf-8", errors="ignore")
        if not char:
            return "unknown"
        if char in {"\r", "\n"}:
            return "enter"
        if char == "\x7f":
            return "backspace"
        if char == "\t":
            return "tab"
        if char == "\x03":
            return "ctrl-c"
        return char.lower()


def _console_confirm(
    session: _TerminalSession,
    state: _Diagnostics,
    selected_key: str,
    scroll: int,
    question: str,
) -> bool:
    """在控制台内显示 y/N 确认框。"""

    width, height = _terminal_size()
    while True:
        lines = _render_console_lines(
            state,
            selected_key,
            scroll,
            width,
            height,
            f"CONFIRM  {question}  [y/N]",
            ["y=confirm  n/Esc=cancel"],
        )
        _draw_lines(lines)
        key = session.read_key()
        if key == "y":
            return True
        if key in {"n", "esc", "q", "ctrl-c"}:
            return False


def _load_help_lines() -> list[str]:
    """读取 docs/deploy/help.txt，返回控制台查看器文本。"""

    if HELP_FILE.exists():
        return HELP_FILE.read_text(encoding="utf-8").splitlines()
    return [
        "Help file not found: " + _rel(HELP_FILE),
        "",
        "Restore docs/deploy/help.txt, then press R to reload.",
    ]


def _collect_log_lines(service: _ServiceInfo) -> list[str]:
    """返回指定服务的最近日志行。"""

    if not service.implemented:
        return ["(ai-service is not implemented)"]
    if service.key == "ecommerce":
        try:
            result = _compose(
                "logs",
                "--tail",
                "200",
                "app",
                capture=True,
                check=False,
                announce=False,
            )
        except DeployError as exc:
            return [f"(cannot read docker logs: {exc})"]
        output = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part
        )
        return output.splitlines() or ["(no logs)"]

    path = DEPLOY_LOG_DIR / f"{service.key}.log"
    if not path.exists() and service.key == "customer":
        configured = env("CUSTOMER_SERVICE_LOG_DIR", "").strip()
        log_dir = (
            Path(configured).expanduser()
            if configured
            else ROOT / "var" / "logs" / "customer-service"
        )
        if not log_dir.is_absolute():
            log_dir = ROOT / log_dir
        candidates = sorted(log_dir.glob("*.log"))
        if candidates:
            path = candidates[-1]
    return _read_log_file(path)


def _read_log_file(path: Path) -> list[str]:
    """读取文本日志；示例：存在 -> 行列表，不存在 -> 提示行。"""

    if not path.exists():
        return [f"(log file not found: {_rel(path)})"]
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines() or [
            "(empty log)"
        ]
    except OSError as exc:
        return [f"(cannot read log: {exc})"]


def _show_text_viewer(
    session: _TerminalSession,
    title: str,
    text_lines: list[str],
    *,
    tail: bool = False,
    refresh: Callable[[], list[str]] | None = None,
) -> None:
    """显示可滚动的帮助或日志查看器，Esc/Q 返回。"""

    width, height = _terminal_size()
    content_height = max(1, height - 6)
    offset = max(0, len(text_lines) - content_height) if tail else 0
    while True:
        max_offset = max(0, len(text_lines) - content_height)
        offset = max(0, min(offset, max_offset))
        end = min(len(text_lines), offset + content_height)
        footer = (
            f"lines {offset + 1}-{end}/{len(text_lines)}  "
            "Up/Down=line  J/K=page  R=refresh  Esc/Q=back"
        )
        _draw_lines(
            _render_viewer_lines(title, text_lines, offset, width, height, footer)
        )
        key = session.read_key()
        if key in {"esc", "q", "ctrl-c"}:
            return
        if key in {"k", "pageup"}:
            offset = max(0, offset - max(1, content_height - 1))
        elif key in {"j", "pagedown"}:
            offset = min(max_offset, offset + max(1, content_height - 1))
        elif key == "up":
            offset = max(0, offset - 1)
        elif key == "down":
            offset = min(max_offset, offset + 1)
        elif key == "home":
            offset = 0
        elif key == "end":
            offset = max_offset
        elif key == "r" and refresh is not None:
            text_lines = refresh()
            offset = max(0, len(text_lines) - content_height) if tail else 0


def _wait_for_enter() -> None:
    """等待用户按 Enter 返回控制台。"""

    try:
        input("\nPress Enter to return to console ...")
    except (EOFError, KeyboardInterrupt):
        pass


def _run_suspended(
    session: _TerminalSession, title: str, action: Callable[[], object]
) -> str:
    """挂起控制台执行动作，返回结果消息。"""

    session.suspend()
    print(f"\n=== {title} ===")
    message = f"{title}: done"
    try:
        result = action()
        if result is False:
            message = f"{title}: skipped"
    except DeployError as exc:
        _error(str(exc))
        message = f"{title}: failed - {exc}"
    except OSError as exc:
        _error(f"system error: {exc}")
        message = f"{title}: failed - {exc}"
    except KeyboardInterrupt:
        print()
        _warn("operation cancelled")
        message = f"{title}: cancelled"
    finally:
        _wait_for_enter()
        session.resume()
    return message


def _frontend_for_key(key: str) -> _Frontend | None:
    """按服务键返回前端配置。"""

    return next((frontend for frontend in _FRONTENDS if frontend.name == key), None)


def _start_service(service: _ServiceInfo) -> Callable[[], object]:
    """返回启动或重启服务的可调用对象。"""

    if service.key == "ecommerce":
        return lambda: deploy_ecommerce_service(rebuild=False)
    if service.key == "customer":
        return deploy_customer_service
    if service.key == "ai":
        return deploy_ai_service
    frontend = _frontend_for_key(service.key)
    if frontend is None:
        raise DeployError(f"unknown frontend: {service.key}")
    return lambda: (_prepare(), _deploy_frontend(frontend))


def _build_service(service: _ServiceInfo) -> Callable[[], object]:
    """返回重建服务的可调用对象。"""

    if service.key == "ecommerce":
        return build_ecommerce_service
    if service.key == "customer":
        return build_customer_service
    if service.key == "ai":
        return build_ai_service
    frontend = _frontend_for_key(service.key)
    if frontend is None:
        raise DeployError(f"unknown frontend: {service.key}")
    return lambda: (_prepare(), _build_frontend(frontend))


def _stop_service(service: _ServiceInfo) -> Callable[[], object]:
    """返回停止服务的可调用对象。"""

    if service.key == "ecommerce":
        return stop_ecommerce_service
    if service.key == "customer":
        return lambda: _stop_process("customer-service", "customer_service.app.app")
    if service.key == "ai":
        return lambda: _warn("ai-service is not implemented")
    frontend = _frontend_for_key(service.key)
    if frontend is None:
        raise DeployError(f"unknown frontend: {service.key}")
    return lambda: _stop_process(frontend.name, "vite")


def _show_service_logs(session: _TerminalSession, service: _ServiceInfo) -> None:
    """打开服务日志查看器。"""

    _show_text_viewer(
        session,
        f"LOGS  {service.label}",
        _collect_log_lines(service),
        tail=True,
        refresh=lambda: _collect_log_lines(service),
    )


def _refresh_state(state: _Diagnostics, selected_key: str) -> tuple[_Diagnostics, str]:
    """重新运行检查并保留仍存在的选择。"""

    state = _collect_diagnostics()
    keys = _entry_keys(state)
    if selected_key not in keys:
        selected_key = keys[0] if keys else ""
    return state, selected_key


def _interactive_console() -> int:
    """运行 ASCII 交互式控制台主循环。"""

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        _error("interactive console requires a TTY; use a subcommand in CI.")
        return 2

    print("Checking dependencies, configuration, ports and services ...")
    state = _collect_diagnostics()
    keys = _entry_keys(state)
    selected_key = keys[0] if keys else ""
    scroll = 0
    message = "Ready. Select a service or action."

    with _TerminalSession() as session:
        while True:
            width, height = _terminal_size()
            _draw_lines(
                _render_console_lines(
                    state,
                    selected_key,
                    scroll,
                    width,
                    height,
                    message,
                )
            )
            key = session.read_key()
            selected_service = _find_service(state, selected_key)
            selected_action = _find_action(selected_key)

            if key in {"q", "esc", "ctrl-c"}:
                return 0
            if key == "up":
                selected_key = _move_selection(state, selected_key, -1)
                scroll = _reveal_selection(state, selected_key, scroll, width, height)
                message = _selection_message(state, selected_key)
            elif key == "down":
                selected_key = _move_selection(state, selected_key, 1)
                scroll = _reveal_selection(state, selected_key, scroll, width, height)
                message = _selection_message(state, selected_key)
            elif key == "left":
                selected_key = _first_service_key(state) or selected_key
                scroll = _reveal_selection(state, selected_key, scroll, width, height)
                message = _selection_message(state, selected_key)
            elif key == "right":
                selected_key = _first_action_key()
                scroll = _reveal_selection(state, selected_key, scroll, width, height)
                message = _selection_message(state, selected_key)
            elif key in {"j", "pagedown"}:
                rows = _console_rows(state, max(1, width - 4), selected_key)
                page = max(1, _console_content_height(height) - 1)
                scroll = min(
                    max(0, len(rows) - _console_content_height(height)),
                    scroll + page,
                )
                message = "Scroll down."
            elif key in {"k", "pageup"}:
                page = max(1, _console_content_height(height) - 1)
                scroll = max(0, scroll - page)
                message = "Scroll up."
            elif key == "home":
                scroll = 0
                message = "Scroll to top."
            elif key == "end":
                rows = _console_rows(state, max(1, width - 4), selected_key)
                scroll = max(0, len(rows) - _console_content_height(height))
                message = "Scroll to bottom."
            elif key == "f":
                message = "Refreshing checks ..."
                _draw_lines(
                    _render_console_lines(
                        state, selected_key, scroll, width, height, message
                    )
                )
                state, selected_key = _refresh_state(state, selected_key)
                message = "Checks refreshed."
            elif key in {"?", "h"}:
                _show_text_viewer(
                    session,
                    "HELP  deploy.py",
                    _load_help_lines(),
                    refresh=_load_help_lines,
                )
            elif key == "a":
                message = _run_suspended(
                    session,
                    "Deploy all",
                    lambda: (
                        _build_selected(["all"]),
                        deploy_all_services(rebuild=False),
                    ),
                )
                state, selected_key = _refresh_state(state, selected_key)
            elif key == "d":
                if _console_confirm(
                    session,
                    state,
                    selected_key,
                    scroll,
                    "Reset all databases? This cannot be undone.",
                ):
                    message = _run_suspended(
                        session,
                        "Reset databases",
                        lambda: reset_all_databases(assume_yes=True),
                    )
                else:
                    message = "Reset cancelled."
                state, selected_key = _refresh_state(state, selected_key)
            elif key == "enter":
                if selected_action is not None:
                    if selected_action.key == "quit":
                        return 0
                    if selected_action.key == "build_all":
                        message = _run_suspended(
                            session,
                            "Build all",
                            lambda: _build_selected(["all"]),
                        )
                    elif selected_action.key == "deploy_all":
                        message = _run_suspended(
                            session,
                            "Deploy all",
                            lambda: (
                                _build_selected(["all"]),
                                deploy_all_services(rebuild=False),
                            ),
                        )
                    elif selected_action.key == "reset_db":
                        if _console_confirm(
                            session,
                            state,
                            selected_key,
                            scroll,
                            "Reset all databases? This cannot be undone.",
                        ):
                            message = _run_suspended(
                                session,
                                "Reset databases",
                                lambda: reset_all_databases(assume_yes=True),
                            )
                        else:
                            message = "Reset cancelled."
                    state, selected_key = _refresh_state(state, selected_key)
                elif selected_service is not None:
                    if not selected_service.implemented:
                        message = "ai-service is not implemented."
                    else:
                        message = _run_suspended(
                            session,
                            f"Start {selected_service.label}",
                            _start_service(selected_service),
                        )
                        state, selected_key = _refresh_state(state, selected_key)
            elif key in {"b", "r", "s", "l"}:
                if selected_service is None:
                    message = "Select a service first."
                    continue
                if not selected_service.implemented:
                    message = "ai-service is not implemented."
                    continue
                if key == "b":
                    message = _run_suspended(
                        session,
                        f"Build {selected_service.label}",
                        _build_service(selected_service),
                    )
                elif key == "r":
                    message = _run_suspended(
                        session,
                        f"Restart {selected_service.label}",
                        _start_service(selected_service),
                    )
                elif key == "s":
                    message = _run_suspended(
                        session,
                        f"Stop {selected_service.label}",
                        _stop_service(selected_service),
                    )
                elif key == "l":
                    _show_service_logs(session, selected_service)
                    message = f"Closed logs for {selected_service.label}."
                state, selected_key = _refresh_state(state, selected_key)
            else:
                message = "Unknown key. Press ? for help."


def _print_help_document() -> None:
    """按 man 风格输出 docs/deploy/help.txt。"""

    if not HELP_FILE.exists():
        raise DeployError(f"帮助文档不存在：{_rel(HELP_FILE)}")
    content = HELP_FILE.read_text(encoding="utf-8").rstrip()
    if not content:
        raise DeployError(f"帮助文档为空：{_rel(HELP_FILE)}")
    print(content)


def _build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="deploy.py",
        description="ShopAgent 一键式部署脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "服务名称：ecommerce、customer、ai、frontend、all\n"
            "不带参数运行进入 ASCII 交互式控制台。\n"
            "使用 python deploy.py help 查看完整部署手册。"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", metavar="命令")

    env_parser = subparsers.add_parser("env", help="从 .env.example 创建 .env")
    env_parser.add_argument("--force", "-f", action="store_true", help="覆盖已有 .env")

    build_parser = subparsers.add_parser("build", help="构建服务")
    build_parser.add_argument(
        "components",
        nargs="*",
        default=["all"],
        metavar="SERVICE",
        help="要构建的服务，默认 all",
    )

    deploy_parser = subparsers.add_parser("deploy", help="部署服务（默认先构建）")
    deploy_parser.add_argument(
        "components",
        nargs="*",
        default=["all"],
        metavar="SERVICE",
        help="要部署的服务，默认 all",
    )
    deploy_parser.add_argument(
        "--skip-build", action="store_true", help="跳过构建，直接使用已有产物"
    )

    status_parser = subparsers.add_parser(
        "status", aliases=["check"], help="查看服务状态"
    )
    status_parser.add_argument(
        "--strict", action="store_true", help="有服务未运行时以非零状态退出"
    )

    undeploy_parser = subparsers.add_parser(
        "undeploy", aliases=["down"], help="停止并卸载全部服务"
    )
    undeploy_parser.add_argument(
        "--volumes", action="store_true", help="同时删除 PostgreSQL / Redis 数据卷"
    )
    undeploy_parser.add_argument("--yes", "-y", action="store_true", help="跳过确认")

    reset_parser = subparsers.add_parser(
        "reset-db", aliases=["reset"], help="重置全部数据库"
    )
    reset_parser.add_argument("--yes", "-y", action="store_true", help="跳过确认")

    subparsers.add_parser("help", aliases=["man"], help="显示部署手册（man 风格）")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """解析参数并分发到构建、部署、状态、卸载、重置或帮助。"""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        if not sys.stdin.isatty():
            parser.print_help()
            return 2
        return _interactive_console()

    command = {
        "check": "status",
        "down": "undeploy",
        "reset": "reset-db",
        "man": "help",
    }.get(args.command, args.command)

    try:
        if command == "env":
            create_env(force=args.force)
            _prepare()
            _ok(f"环境配置检查通过：{_rel(ENV_FILE)}")
        elif command == "build":
            _build_selected(args.components)
        elif command == "deploy":
            if not args.skip_build:
                _build_selected(args.components)
            _deploy_selected(args.components, rebuild=False)
        elif command == "status":
            all_up = check_status()
            if args.strict and not all_up:
                return 1
        elif command == "undeploy":
            if (
                args.volumes
                and not args.yes
                and not _confirm("确认删除数据卷吗？该操作不可恢复。")
            ):
                _info("已取消")
                return 0
            undeploy_all_services(remove_volumes=args.volumes)
        elif command == "reset-db":
            reset_all_databases(assume_yes=args.yes)
        elif command == "help":
            _print_help_document()
        else:
            parser.print_help()
            return 2
    except DeployError as exc:
        _error(str(exc))
        return 1
    except OSError as exc:
        _error(f"系统错误：{exc}")
        return 1
    except KeyboardInterrupt:
        print()
        _warn("操作已取消")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
