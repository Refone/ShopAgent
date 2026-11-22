"""ShopAgent 一键式部署脚本。

不带参数运行时进入交互式菜单；带子命令时用于 CI 等非交互场景，例如：

    python deploy.py deploy
    python deploy.py build frontend
    python deploy.py status --strict
    python deploy.py reset-db --yes
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

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

_ENV_CACHE: dict[str, str] | None = None
_DOCKER_READY = False


class DeployError(RuntimeError):
    """可预期的部署错误，由入口统一打印。"""


def _paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR_ENABLED else text


def _section(title: str) -> None:
    print(f"\n{_paint(f'== {title} ==', '1;36')}")


def _info(message: str) -> None:
    print(f"{_paint('[deploy]', '36')} {message}")


def _ok(message: str) -> None:
    print(f"{_paint('[  ok  ]', '32')} {message}")


def _warn(message: str) -> None:
    print(f"{_paint('[ warn ]', '33')} {message}")


def _error(message: str) -> None:
    print(f"{_paint('[ fail ]', '31')} {message}", file=sys.stderr)


def _rel(path: Path) -> str:
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


# ---------------------------------------------------------------------------
# 环境变量与配置
# ---------------------------------------------------------------------------


def _parse_env_line(line: str) -> tuple[str, str] | None:
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
    value = load_env().get(key)
    return default if value is None or value == "" else value


def env_int(key: str, default: int) -> int:
    raw = env(key, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise DeployError(f"环境变量 {key} 必须是整数，当前值为 {raw!r}") from exc


def _validate_config() -> None:
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
    if shutil.which(command) is None:
        raise DeployError(f"未找到命令 {command!r}。{hint}")


def _require_docker() -> None:
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
    result = _compose("ps", "-q", service, capture=True, check=False, announce=False)
    return result.returncode == 0 and bool(result.stdout.strip())


def _compose_image_exists(service: str) -> bool:
    result = _compose(
        "images", "-q", service, capture=True, check=False, announce=False
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _probe_host(host: str) -> str:
    if host in {"", "*", "0.0.0.0", "::", "[::]"}:
        return "127.0.0.1"
    return host


def _url_host(host: str) -> str:
    host = _probe_host(host)
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _http_url(host: str, port: int, path: str = "") -> str:
    if path and not path.startswith("/"):
        path = "/" + path
    return f"http://{_url_host(host)}:{port}{path}"


def _display_url(host: str, port: int, path: str = "") -> str:
    return _http_url(host, port, path)


def _http_ok(url: str, timeout: float = 2.0) -> bool:
    request = urllib.request.Request(
        url, headers={"User-Agent": "shopagent-deploy/1.0"}
    )
    try:
        with _HTTP_OPENER.open(request, timeout=timeout) as response:
            return 200 <= getattr(response, "status", 0) < 400
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _tcp_ok(host: str, port: int) -> bool:
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
    return RUN_DIR / f"{name}.pid"


def _read_pid(name: str) -> int | None:
    path = _pid_file(name)
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return int(raw)
    except (OSError, ValueError):
        return None


def _write_pid(name: str, pid: int) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    _pid_file(name).write_text(f"{pid}\n", encoding="utf-8")


def _remove_pid(name: str) -> None:
    try:
        _pid_file(name).unlink()
    except FileNotFoundError:
        pass


def _pid_alive(pid: int) -> bool:
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
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.2)
    return not _pid_alive(pid)


def _stop_process(name: str, marker: str | None = None) -> bool:
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
    if _tcp_ok(host, port):
        raise DeployError(
            f"{name} 端口 {port} 已被占用，请先停止占用该端口的进程"
            "（例如手动启动的服务或 npm run dev）。"
        )


def _start_background(
    name: str, command: Sequence[str | Path], cwd: Path
) -> tuple[subprocess.Popen, Path]:
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
    candidates = (
        CUSTOMER_DIR / ".venv" / "bin" / "python",
        CUSTOMER_DIR / ".venv" / "Scripts" / "python.exe",
    )
    return next((path for path in candidates if path.exists()), None)


def _customer_uvicorn() -> Path | None:
    candidates = (
        CUSTOMER_DIR / ".venv" / "bin" / "uvicorn",
        CUSTOMER_DIR / ".venv" / "Scripts" / "uvicorn.exe",
    )
    return next((path for path in candidates if path.exists()), None)


def _ensure_customer_build() -> None:
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
    bin_dir = directory / "node_modules" / ".bin"
    for name in ("vite", "vite.cmd"):
        candidate = bin_dir / name
        if candidate.exists():
            return candidate
    return None


def _build_frontend(frontend: _Frontend) -> None:
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
    return env(f"{frontend.env_prefix}_HOST", "127.0.0.1")


def _frontend_port(frontend: _Frontend) -> int:
    return env_int(f"{frontend.env_prefix}_PORT", frontend.default_port)


def _deploy_frontend(frontend: _Frontend) -> None:
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


def _status_symbol(ok: bool) -> str:
    if ok:
        return _paint("✓", "32")
    return _paint("✗", "31")


def _status_line(label: str, ok: bool, detail: str) -> None:
    print(f"  {_status_symbol(ok)} {label:<22} {detail}")


def check_status() -> bool:
    """
    检查服务状态
    """

    _section("服务状态")
    _prepare()

    ecommerce_host = env("ECOMMERCE_SERVICE_HOST", "127.0.0.1")
    ecommerce_port = env_int("ECOMMERCE_SERVICE_PORT", 18001)
    ecommerce_url = _display_url(ecommerce_host, ecommerce_port)
    ecommerce_up = _http_ok(_http_url(ecommerce_host, ecommerce_port, "/health"))
    if shutil.which("docker") is not None:
        result = _compose("ps", "-q", capture=True, check=False, announce=False)
        containers = len(result.stdout.splitlines()) if result.returncode == 0 else 0
        ecommerce_detail = (
            f"{ecommerce_url}（{containers} 个容器运行中）"
            if ecommerce_up
            else f"{ecommerce_url}（未就绪，容器 {containers} 个）"
        )
    else:
        ecommerce_detail = f"{ecommerce_url}（未安装 docker）"
    _status_line("ecommerce-service", ecommerce_up, ecommerce_detail)

    customer_host = env("CUSTOMER_SERVICE_HOST", "127.0.0.1")
    customer_port = env_int("CUSTOMER_SERVICE_PORT", 18000)
    customer_url = _display_url(customer_host, customer_port)
    customer_pid = _tracked_pid("customer-service", "customer_service.app.app")
    customer_up = _http_ok(_http_url(customer_host, customer_port, "/openapi.json"))
    if customer_up:
        customer_detail = customer_url
    elif customer_pid is not None:
        customer_detail = f"{customer_url}（进程运行中，接口未就绪）"
    else:
        customer_detail = f"{customer_url}（未运行）"
    _status_line("customer-service", customer_up, customer_detail)

    print(f"  {_paint('–', '90')} {'ai-service':<22} 尚未实现，跳过")

    frontend_status: list[bool] = []
    for frontend in _FRONTENDS:
        host = _frontend_host(frontend)
        port = _frontend_port(frontend)
        url = _display_url(host, port)
        pid = _tracked_pid(frontend.name, "vite")
        up = _http_ok(_http_url(host, port, "/"))
        frontend_status.append(up)
        if up:
            detail = url
        elif pid is not None:
            detail = f"{url}（进程运行中，页面未就绪）"
        else:
            detail = f"{url}（未运行）"
        _status_line(frontend.name, up, detail)

    print()
    print(f"  日志目录：{_rel(DEPLOY_LOG_DIR)}")
    return all([ecommerce_up, customer_up, *frontend_status])


# ---------------------------------------------------------------------------
# 交互式菜单与命令行入口
# ---------------------------------------------------------------------------


def _confirm(question: str, assume_yes: bool = False) -> bool:
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


_MENU = """
请选择操作：
  1. 初始化环境（创建 .env、检查依赖）
  2. 构建全部服务
  3. 一键部署（构建 + 启动全部）
  4. 查看服务状态
  5. 重置全部数据库
  6. 停止并卸载全部服务
  7. 单独构建 / 部署服务
  0. 退出
"""

_SERVICE_MENU = """
请选择操作：
  1. 构建 ecommerce-service
  2. 构建 customer-service
  3. 构建 frontend
  4. 部署 ecommerce-service
  5. 部署 customer-service
  6. 部署 frontend
  0. 返回
"""


def _interactive_service_menu() -> None:
    print(_SERVICE_MENU)
    try:
        choice = input("请输入序号：").strip()
    except EOFError:
        return

    if choice == "1":
        build_ecommerce_service()
    elif choice == "2":
        build_customer_service()
    elif choice == "3":
        build_frontend()
    elif choice == "4":
        deploy_ecommerce_service()
    elif choice == "5":
        deploy_customer_service()
    elif choice == "6":
        deploy_frontend()
    elif choice == "0":
        return
    else:
        _warn("无效的序号")


def _interactive_menu() -> int:
    while True:
        print(_MENU)
        try:
            choice = input("请输入序号：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if choice == "0":
            return 0

        try:
            if choice == "1":
                create_env()
                ensure_environment()
            elif choice == "2":
                build_all_services()
            elif choice == "3":
                build_all_services()
                deploy_all_services(rebuild=False)
            elif choice == "4":
                check_status()
            elif choice == "5":
                reset_all_databases()
            elif choice == "6":
                remove_volumes = _confirm("是否同时删除数据卷？")
                undeploy_all_services(remove_volumes=remove_volumes)
            elif choice == "7":
                _interactive_service_menu()
            else:
                _warn("无效的序号")
        except DeployError as exc:
            _error(str(exc))
        except OSError as exc:
            _error(f"系统错误：{exc}")
        except KeyboardInterrupt:
            print()
            _warn("操作已取消")

        try:
            input("\n按回车返回菜单 ...")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deploy.py",
        description="ShopAgent 一键式部署脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "服务名称：ecommerce、customer、ai、frontend、all\n"
            "不带参数运行进入交互式菜单。"
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

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        if not sys.stdin.isatty():
            parser.print_help()
            return 2
        return _interactive_menu()

    command = {
        "check": "status",
        "down": "undeploy",
        "reset": "reset-db",
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
