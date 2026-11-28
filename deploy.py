#!/usr/bin/env python3
"""ShopAgent 一键式部署脚本。

不带参数运行时进入 htop 风格的 TUI；带子命令时用于 CI 等非交互场景，例如：

    python deploy.py deploy
    python deploy.py build frontend
    python deploy.py status --strict
    python deploy.py reset-db --yes
"""

from __future__ import annotations

import argparse
import json
import locale
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

try:
    import curses
except ImportError:  # 例如未安装 windows-curses 的 Windows
    curses = None  # type: ignore[assignment]

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
TMP_DIR = ROOT / "var" / "tmp"
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

# 线程上下文：TUI 的任务线程在这里挂载日志出口（sink）和当前任务（job），
# 命令行模式下两者都为空，输出直接打印到终端。
_CTX = threading.local()
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


class DeployError(RuntimeError):
    """可预期的部署错误，由入口统一打印。"""


def _emit(text: str = "", *, err: bool = False) -> None:
    """输出文本：任务线程中写入任务日志，否则打印到终端。"""

    sink = getattr(_CTX, "sink", None)
    if sink is None:
        print(text, file=sys.stderr if err else sys.stdout)
        return
    for line in _ANSI_RE.sub("", text).splitlines() or [""]:
        sink(line)


def _paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR_ENABLED else text


def _section(title: str) -> None:
    _emit(f"\n{_paint(f'== {title} ==', '1;36')}")


def _info(message: str) -> None:
    _emit(f"{_paint('[deploy]', '36')} {message}")


def _ok(message: str) -> None:
    _emit(f"{_paint('[  ok  ]', '32')} {message}")


def _warn(message: str) -> None:
    _emit(f"{_paint('[ warn ]', '33')} {message}")


def _error(message: str) -> None:
    _emit(f"{_paint('[ fail ]', '31')} {message}", err=True)


def _check_cancel() -> None:
    """当前任务被用户取消时抛出 DeployError，供等待循环及时退出。"""

    job = getattr(_CTX, "job", None)
    if job is not None and job.cancelled:
        raise DeployError("任务已取消")


def _kill_process_group(process: subprocess.Popen, sig: int = signal.SIGTERM) -> None:
    """向子进程所在进程组发送信号。"""

    try:
        if hasattr(os, "killpg"):
            os.killpg(process.pid, sig)
        else:
            process.terminate()
    except (ProcessLookupError, PermissionError):
        pass


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


def _run_streamed(
    args: list[str],
    cwd: Path | None,
    env_values: dict[str, str] | None,
    timeout: float | None,
) -> subprocess.CompletedProcess[str]:
    """TUI 任务线程中执行命令：输出逐行写入任务日志，支持取消与超时。"""

    sink = _CTX.sink
    job = getattr(_CTX, "job", None)
    process = subprocess.Popen(
        args,
        cwd=str(cwd) if cwd is not None else None,
        env=env_values,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
        start_new_session=True,
    )
    timed_out = threading.Event()

    def _on_timeout() -> None:
        timed_out.set()
        _kill_process_group(process)

    timer = threading.Timer(timeout, _on_timeout) if timeout else None
    if timer is not None:
        timer.start()
    if job is not None:
        job.process = process
        if job.cancelled:
            _kill_process_group(process)

    try:
        assert process.stdout is not None
        for line in process.stdout:
            sink(_ANSI_RE.sub("", line.rstrip("\r\n")))
        process.wait()
    finally:
        if timer is not None:
            timer.cancel()
        if job is not None:
            job.process = None

    if job is not None and job.cancelled:
        raise DeployError("任务已取消")
    if timed_out.is_set():
        raise DeployError(f"命令执行超时：{shlex.join(args)}")
    return subprocess.CompletedProcess(args, process.returncode, "", "")


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

    in_tui = getattr(_CTX, "sink", None) is not None
    try:
        if in_tui and not capture:
            completed = _run_streamed(args, cwd, env_values, timeout)
        else:
            completed = subprocess.run(
                args,
                cwd=str(cwd) if cwd is not None else None,
                env=env_values,
                stdin=subprocess.DEVNULL if in_tui else None,
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
        _check_cancel()
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
        _check_cancel()
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
        _emit("\n".join(tail))


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


def _listening_pids(port: int) -> list[int]:
    lsof = shutil.which("lsof")
    if lsof is None:
        raise DeployError("恢复丢失的 PID 文件需要 lsof，但当前系统未找到 lsof。")
    try:
        result = subprocess.run(
            [lsof, "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeployError(f"查询端口 {port} 的监听进程失败：{exc}") from exc
    if result.returncode not in {0, 1}:
        detail = result.stderr.strip() or f"退出码 {result.returncode}"
        raise DeployError(f"查询端口 {port} 的监听进程失败：{detail}")
    return [int(item) for item in result.stdout.split() if item.isdigit()]


def _process_home(name: str) -> tuple[Path, str, int, tuple[str, ...]] | None:
    """返回后台进程所属目录、监听地址，以及认领时命令行必须包含的标识。"""

    if name == "customer-service":
        host = env("CUSTOMER_SERVICE_HOST", "127.0.0.1")
        port = env_int("CUSTOMER_SERVICE_PORT", 18000)
        return CUSTOMER_DIR, host, port, ("customer_service.app.app",)
    if name == "ai-service":
        host = env("AI_SERVICE_HOST", "127.0.0.1")
        port = env_int("AI_SERVICE_PORT", 18002)
        return AI_DIR, host, port, ("ai_service.app.app",)
    for frontend in _FRONTENDS:
        if frontend.name == name:
            host, port = _frontend_host(frontend), _frontend_port(frontend)
            # 只认领 deploy 启动的 vite preview，不碰手动运行的 npm run dev。
            return frontend.directory, host, port, ("vite", "preview")
    return None


def _recover_pid(name: str) -> int | None:
    """PID 文件丢失时，认领本检出启动、仍在监听端口的进程。"""

    home = _process_home(name)
    if home is None:
        return None
    directory, host, port, tokens = home
    if not _tcp_ok(host, port):
        return None
    prefix = f"{directory}{os.sep}"
    for pid in _listening_pids(port):
        command = _pid_command(pid)
        if prefix in command and all(token in command for token in tokens):
            _write_pid(name, pid)
            _warn(f"{name} 的 PID 文件已丢失，已认领本检出的进程 {pid}")
            return pid
    return None


def _tracked_pid(name: str, marker: str | None = None) -> int | None:
    pid = _read_pid(name)
    if pid is None:
        return _recover_pid(name)
    if not _pid_alive(pid):
        _remove_pid(name)
        return _recover_pid(name)

    command = _pid_command(pid)
    if marker and command and marker not in command:
        _warn(f"PID 文件 {_pid_file(name).name} 指向的进程 {pid} 与预期不符，已忽略")
        _remove_pid(name)
        return _recover_pid(name)
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
        # 同一个 TUI 启动的 preview 退出后可能还在等待父进程回收。
        if os.name == "posix":
            try:
                if os.waitpid(pid, os.WNOHANG)[0] == pid:
                    return True
            except ChildProcessError:
                pass
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
        if not _pid_gone(pid, 3.0):
            raise DeployError(f"{name}（PID {pid}）未停止。")
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
                stdin=subprocess.DEVNULL,
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


def _pypi_index_url() -> str:
    """返回安装 Python 依赖使用的 PyPI 镜像地址。

    只由 .env 的 PIP_MIRROR_HOST（域名）决定，默认公网阿里云；
    腾讯云服务器可改为 mirrors.tencentyun.com（内网，4-7 MB/s）。
    """

    return f"https://{env('PIP_MIRROR_HOST', 'mirrors.aliyun.com')}/pypi/simple/"


def build_customer_service() -> bool:
    """
    构建客户服务
    """

    _prepare()
    _require_command("uv", "请先安装 uv：https://docs.astral.sh/uv/")
    _section("构建 customer-service")

    python = _customer_venv_python()
    if python is None:
        _run(["uv", "venv", "--python", "3.12"], cwd=CUSTOMER_DIR)
        python = _customer_venv_python()
        if python is None:
            raise DeployError("customer-service 虚拟环境创建失败，请检查 uv 输出。")

    # uv.lock 里的下载地址是写死的 files.pythonhosted.org，`uv sync --frozen` 不会改走
    # 镜像源（国内实测约 15 KB/s，会卡死）。这里先导出只含版本与哈希的锁定清单，
    # 再按镜像地址安装：版本仍然严格锁定，下载走国内源。
    requirements = TMP_DIR / "customer-service-requirements.txt"
    requirements.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "uv",
            "export",
            "--frozen",
            "--format",
            "requirements-txt",
            "--output-file",
            str(requirements),
        ],
        cwd=CUSTOMER_DIR,
    )
    _run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--index-url",
            _pypi_index_url(),
            "--requirements",
            str(requirements),
        ],
        cwd=CUSTOMER_DIR,
    )

    if _customer_venv_python() is None or _customer_uvicorn() is None:
        raise DeployError("customer-service 虚拟环境创建失败，请检查依赖安装输出。")
    _ok("customer-service 依赖安装完成")
    return True


def _ai_venv_python() -> Path | None:
    candidates = (
        AI_DIR / ".venv" / "bin" / "python",
        AI_DIR / ".venv" / "Scripts" / "python.exe",
    )
    return next((path for path in candidates if path.exists()), None)


def _ai_uvicorn() -> Path | None:
    candidates = (
        AI_DIR / ".venv" / "bin" / "uvicorn",
        AI_DIR / ".venv" / "Scripts" / "uvicorn.exe",
    )
    return next((path for path in candidates if path.exists()), None)


def _ensure_ai_build() -> None:
    if _ai_venv_python() is None or _ai_uvicorn() is None:
        build_ai_service()


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
        _warn("ai-service 目录为空，跳过构建")
        return False

    _require_command("uv", "请先安装 uv：https://docs.astral.sh/uv/")
    _section("构建 ai-service")

    python = _ai_venv_python()
    if python is None:
        _run(["uv", "venv", "--python", "3.12"], cwd=AI_DIR)
        python = _ai_venv_python()
        if python is None:
            raise DeployError("ai-service 虚拟环境创建失败，请检查 uv 输出。")

    # 与 customer-service 相同：uv.lock 里的下载地址写死为 files.pythonhosted.org，
    # 先导出只含版本与哈希的清单，再按 .env 的镜像地址安装。
    requirements = TMP_DIR / "ai-service-requirements.txt"
    requirements.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "uv",
            "export",
            "--frozen",
            "--format",
            "requirements-txt",
            "--output-file",
            str(requirements),
        ],
        cwd=AI_DIR,
    )
    _run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--index-url",
            _pypi_index_url(),
            "--requirements",
            str(requirements),
        ],
        cwd=AI_DIR,
    )

    if _ai_venv_python() is None or _ai_uvicorn() is None:
        raise DeployError("ai-service 虚拟环境创建失败，请检查依赖安装输出。")
    _ok("ai-service 依赖安装完成")
    return True


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
    ai_was_running = _tracked_pid("ai-service", "ai_service.app.app") is not None
    if ai_was_running:
        _stop_process("ai-service", "ai_service.app.app")

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
    if ai_was_running:
        _info("恢复重置前正在运行的 ai-service ...")
        deploy_ai_service()
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
        _emit(output)


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
        _warn("ai-service 目录为空，跳过部署")
        return False
    _ensure_ai_build()

    host = env("AI_SERVICE_HOST", "127.0.0.1")
    port = env_int("AI_SERVICE_PORT", 18002)
    uvicorn = _ai_uvicorn()
    if uvicorn is None:
        raise DeployError("ai-service 虚拟环境中找不到 uvicorn。")

    _section("部署 ai-service")
    _stop_process("ai-service", "ai_service.app.app")
    _ensure_port_free("ai-service", host, port)

    process, log_path = _start_background(
        "ai-service",
        [
            uvicorn,
            "ai_service.app.app:app",
            "--host",
            host,
            "--port",
            str(port),
        ],
        AI_DIR,
    )
    try:
        _wait_http(
            "ai-service",
            _http_url(host, port, "/health"),
            timeout=90.0,
            process=process,
            log_path=log_path,
        )
    except DeployError:
        _stop_process("ai-service", "ai_service.app.app")
        raise

    _ok(f"ai-service：{_display_url(host, port)}")
    _ok(f"接口文档：{_display_url(host, port, '/docs')}")
    return True


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
    # customer-service 的 TurnWorker 依赖 ai-service，先部署 ai-service
    deploy_ai_service()
    deploy_customer_service()
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
    _stop_process("ai-service", "ai_service.app.app")
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
        _emit(
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

    ai_host = env("AI_SERVICE_HOST", "127.0.0.1")
    ai_port = env_int("AI_SERVICE_PORT", 18002)
    ai_url = _display_url(ai_host, ai_port)
    ai_pid = _tracked_pid("ai-service", "ai_service.app.app")
    ai_up = _http_ok(_http_url(ai_host, ai_port, "/health"))
    if ai_up:
        ai_detail = ai_url
    elif ai_pid is not None:
        ai_detail = f"{ai_url}（进程运行中，接口未就绪）"
    else:
        ai_detail = f"{ai_url}（未运行）"
    _status_line("ai-service", ai_up, ai_detail)

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
    return all([ecommerce_up, customer_up, ai_up, *frontend_status])


# ---------------------------------------------------------------------------
# 命令行入口
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


# ---------------------------------------------------------------------------
# TUI：服务清单与单行动作
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Target:
    key: str
    name: str
    log_name: str


@dataclass(frozen=True)
class _Spec(_Target):
    """Status 页的一行：一个可构建、部署、卸载的服务。"""

    kind: str  # ecommerce | customer | ai | frontend | infra
    service: str = ""  # compose 服务名
    frontend: _Frontend | None = None


_SPECS = (
    _Spec("ecommerce", "ecommerce-service", "ecommerce-service", "ecommerce", "app"),
    _Spec("customer", "customer-service", "customer-service", "customer"),
    _Spec("ai", "ai-service", "ai-service", "ai"),
    _Spec(
        "user-frontend",
        "frontend-user",
        "user-frontend",
        "frontend",
        frontend=_FRONTENDS[0],
    ),
    _Spec(
        "admin-frontend",
        "frontend-admin",
        "admin-frontend",
        "frontend",
        frontend=_FRONTENDS[1],
    ),
    _Spec("redis", "ecommerce-redis", "ecommerce-redis", "infra", "redis"),
    _Spec("postgres", "ecommerce-pg", "ecommerce-postgres", "infra", "postgres"),
)
_SPEC_BY_KEY = {spec.key: spec for spec in _SPECS}
_DEPLOY_ORDER = (
    "postgres",
    "redis",
    "ecommerce",
    "ai",
    "customer",
    "user-frontend",
    "admin-frontend",
)
_UNINSTALL_ORDER = tuple(reversed(_DEPLOY_ORDER))

# 行 -> (主机变量, 端口变量, 默认端口)
_ENDPOINT_ENV = {
    "ecommerce": ("ECOMMERCE_SERVICE_HOST", "ECOMMERCE_SERVICE_PORT", 18001),
    "customer": ("CUSTOMER_SERVICE_HOST", "CUSTOMER_SERVICE_PORT", 18000),
    "ai": ("AI_SERVICE_HOST", "AI_SERVICE_PORT", 18002),
    "user-frontend": ("USER_FRONTEND_HOST", "USER_FRONTEND_PORT", 18100),
    "admin-frontend": ("ADMIN_FRONTEND_HOST", "ADMIN_FRONTEND_PORT", 18101),
    "postgres": ("POSTGRES_HOST", "POSTGRES_PORT", 18200),
    "redis": ("", "REDIS_PORT", 18201),
}
_HEALTH_PATH = {
    "ecommerce": "/health",
    "customer": "/openapi.json",
    "ai": "/health",
    "user-frontend": "/",
    "admin-frontend": "/",
}

# compose 服务 -> (镜像, 容器名)；compose config 不可用时作为回退
_COMPOSE_DEFAULTS = {
    "app": ("ecommerce-customer-service-app:latest", "ecommerce-app"),
    "postgres": ("pgvector/pgvector:pg16", "ecommerce-postgres"),
    "redis": ("redis:7.4-alpine", "ecommerce-redis"),
}
_COMPOSE_META: dict[str, tuple[str, str]] | None = None


def _discard(_line: str) -> None:
    """静默的日志出口，用于 UI 线程与探测线程。"""


def _silence() -> None:
    _CTX.sink = _discard


def _set_phase(phase: str) -> None:
    job = getattr(_CTX, "job", None)
    if job is not None:
        job.phase = phase


def _image_ref(image: str) -> str:
    name = image.rsplit("/", 1)[-1]
    return image if ":" in name else f"{image}:latest"


def _compose_meta(refresh: bool = False) -> dict[str, tuple[str, str]]:
    """通过 docker compose config 读取各服务的镜像与容器名。"""

    global _COMPOSE_META
    if _COMPOSE_META is not None and not refresh:
        return _COMPOSE_META

    meta = dict(_COMPOSE_DEFAULTS)
    env_file = ENV_FILE if ENV_FILE.exists() else ENV_EXAMPLE
    try:
        result = _run(
            [
                "docker",
                "compose",
                "--env-file",
                env_file,
                "-f",
                COMPOSE_FILE,
                "config",
                "--format",
                "json",
            ],
            cwd=ECOMMERCE_DIR,
            capture=True,
            check=False,
            announce=False,
            timeout=20,
        )
        data = json.loads(result.stdout) if result.returncode == 0 else {}
    except (DeployError, ValueError):
        data = {}

    project = data.get("name") or "ecommerce-customer-service"
    for service, conf in (data.get("services") or {}).items():
        image = conf.get("image") or f"{project}-{service}"
        container = conf.get("container_name") or f"{project}-{service}-1"
        meta[service] = (_image_ref(image), container)
    _COMPOSE_META = meta
    return meta


def _endpoint(spec: _Spec) -> tuple[str, int]:
    host_key, port_key, default = _ENDPOINT_ENV[spec.key]
    host = env(host_key, "127.0.0.1") if host_key else "127.0.0.1"
    return host, env_int(port_key, default)


def _row_url(spec: _Spec, host: str, port: int) -> str:
    if spec.key == "postgres":
        return f"postgresql://{_url_host(host)}:{port}"
    if spec.key == "redis":
        return f"redis://{_url_host(host)}:{port}"
    return _display_url(host, port)


def _docker_text(spec: _Spec, meta: dict[str, tuple[str, str]]) -> str:
    if spec.kind == "ecommerce":
        return ", ".join(meta[service][0] for service in ("app", "postgres", "redis"))
    if spec.kind == "customer":
        return meta["postgres"][0]
    if spec.kind == "infra":
        return meta[spec.service][0]
    return ""


def _image_exists(image: str) -> bool:
    result = _run(
        ["docker", "image", "inspect", image],
        capture=True,
        check=False,
        announce=False,
        timeout=15,
    )
    return result.returncode == 0


def _built_locally(spec: _Spec) -> bool:
    if spec.kind == "customer":
        return _customer_venv_python() is not None and _customer_uvicorn() is not None
    if spec.kind == "ai":
        return _ai_venv_python() is not None and _ai_uvicorn() is not None
    if spec.frontend is not None:
        return (
            spec.frontend.directory / "dist" / "index.html"
        ).exists() and _vite_binary(spec.frontend.directory) is not None
    return False


def _row_built(spec: _Spec) -> bool:
    if spec.kind in {"ecommerce", "infra"}:
        _require_docker()
        return _image_exists(_compose_meta()[spec.service][0])
    return _built_locally(spec)


def _wait_redis_container(timeout: float = 60.0) -> None:
    _info("等待 Redis 容器就绪 ...")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _check_cancel()
        result = _compose(
            "exec",
            "-T",
            "redis",
            "redis-cli",
            "ping",
            capture=True,
            check=False,
            announce=False,
        )
        if result.returncode == 0 and "PONG" in result.stdout:
            _ok("Redis 已就绪")
            return
        time.sleep(1.0)
    raise DeployError(f"Redis 在 {timeout:.0f} 秒内未就绪。")


def _pull_infra(spec: _Spec) -> None:
    _prepare()
    _require_docker()
    image = _compose_meta()[spec.service][0]
    _section(f"拉取 {spec.name} 镜像")
    result = _compose("pull", spec.service, check=False)
    if result.returncode == 0:
        _ok(f"{image} 已是最新")
    elif _image_exists(image):
        _warn(f"镜像拉取失败，继续使用本地镜像 {image}")
    else:
        raise DeployError(f"镜像 {image} 拉取失败，且本地不存在。")


def _deploy_infra(spec: _Spec) -> None:
    _prepare()
    _require_docker()
    _section(f"部署 {spec.name}")
    _compose("up", "-d", spec.service)
    if spec.service == "postgres":
        _wait_postgres_container()
    else:
        _wait_redis_container()
    host, port = _endpoint(spec)
    _ok(f"{spec.name}：{_row_url(spec, host, port)}")


def _build_row(spec: _Spec) -> None:
    if spec.kind == "ecommerce":
        build_ecommerce_service()
    elif spec.kind == "customer":
        build_customer_service()
    elif spec.kind == "ai":
        build_ai_service()
    elif spec.frontend is not None:
        _prepare()
        _require_command("node", "请安装 Node.js 18 或更高版本。")
        _require_command("npm", "请安装 npm。")
        _build_frontend(spec.frontend)
    elif spec.kind == "infra":
        _pull_infra(spec)
    else:
        raise DeployError(f"{spec.name} 尚未实现")


def _deploy_row(spec: _Spec, force_build: bool) -> None:
    """部署单行；force_build 为假时仅在缺少构建产物时先构建。"""

    if spec.kind == "ecommerce":
        _prepare()
        if force_build or not _row_built(spec):
            _set_phase("Building")
            build_ecommerce_service()
            _set_phase("Deploying")
        deploy_ecommerce_service(rebuild=False)
    elif spec.kind == "customer":
        _prepare()
        if force_build or not _row_built(spec):
            _set_phase("Building")
            build_customer_service()
            _set_phase("Deploying")
        deploy_customer_service()
    elif spec.kind == "ai":
        _prepare()
        if force_build or not _row_built(spec):
            _set_phase("Building")
            build_ai_service()
            _set_phase("Deploying")
        deploy_ai_service()
    elif spec.frontend is not None:
        _prepare()
        if force_build or not _row_built(spec):
            _set_phase("Building")
            _build_row(spec)
            _set_phase("Deploying")
        _section(f"部署 {spec.frontend.name}")
        _deploy_frontend(spec.frontend)
    elif spec.kind == "infra":
        _prepare()
        if force_build:
            _set_phase("Building")
            _pull_infra(spec)
            _set_phase("Deploying")
        _deploy_infra(spec)
    else:
        raise DeployError(f"{spec.name} 尚未实现")


def _row_process(spec: _Spec) -> tuple[str, str] | None:
    """返回由本检出以后台进程方式管理的行对应的 PID 名称与命令标识。"""

    if spec.kind == "customer":
        return "customer-service", "customer_service.app.app"
    if spec.kind == "ai":
        return "ai-service", "ai_service.app.app"
    if spec.frontend is not None:
        return spec.frontend.name, "vite"
    return None


def _uninstall_row(spec: _Spec, final: bool = False, volumes: bool = False) -> None:
    """卸载单行；final 表示这是批量卸载的最后一项，改用 compose down 收尾。"""

    _section(f"卸载 {spec.name}")
    process = _row_process(spec)
    if process is not None:
        stopped = _stop_process(*process)
        _ensure_port_free(spec.name, *_endpoint(spec))
        if not stopped:
            _info(f"{process[0]} 未在运行")
    elif spec.kind in {"ecommerce", "infra"}:
        _prepare()
        _require_docker()
        if final:
            args = ["down", "--remove-orphans"]
            if volumes:
                args.append("-v")
            _compose(*args)
        else:
            _compose("rm", "-sf", spec.service)
        if final:
            if volumes:
                _warn("PostgreSQL / Redis 数据卷已删除")
            else:
                _info("PostgreSQL / Redis 数据卷已保留，下次部署会继续使用")
    else:
        raise DeployError(f"{spec.name} 尚未实现")
    _ok(f"{spec.name} 已卸载")


# ---------------------------------------------------------------------------
# TUI：状态探测
# ---------------------------------------------------------------------------


@dataclass
class _RowInfo:
    state: str = "unknown"  # online | ready | failed | missing | unknown
    url: str = ""
    port: int = 0
    listening: bool = False


@dataclass(frozen=True)
class _Item:
    state: str
    main: str
    sub: str = ""
    target: _Target | None = None

    @classmethod
    def dependency(cls, key: str, state: str, name: str, detail: str = "") -> _Item:
        key = f"env-{key}"
        return cls(state, name, detail, _Target(key, name, key))


@dataclass
class _DockerInfo:
    probed: bool = False
    ok: bool = False
    containers: dict[str, str] = field(default_factory=dict)
    images: set[str] = field(default_factory=set)


def _docker_info() -> _DockerInfo:
    info = _DockerInfo(probed=True)
    try:
        containers = _run(
            ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}"],
            capture=True,
            check=False,
            announce=False,
            timeout=8,
        )
        if containers.returncode != 0:
            return info
        images = _run(
            ["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"],
            capture=True,
            check=False,
            announce=False,
            timeout=8,
        )
    except DeployError:
        return info

    for line in containers.stdout.splitlines():
        name, _, state = line.partition("\t")
        if name:
            info.containers[name] = state.strip()
    if images.returncode == 0:
        info.images = {line.strip() for line in images.stdout.splitlines() if line}
    info.ok = True
    return info


def _probe_row(
    spec: _Spec, docker: _DockerInfo, meta: dict[str, tuple[str, str]]
) -> _RowInfo:
    host, port = _endpoint(spec)
    info = _RowInfo(url=_row_url(spec, host, port), port=port)
    info.listening = _tcp_ok(host, port)

    if spec.kind == "infra":
        image, container = meta[spec.service]
        if info.listening:
            running = docker.containers.get(container) == "running"
            info.state = "online" if running else "failed"
        elif container in docker.containers or image in docker.images:
            info.state = "ready"
        else:
            info.state = "missing"
        return info

    process = _row_process(spec)
    if info.listening:
        if process is not None and _tracked_pid(*process) is None:
            info.state = "failed"
            info.url = f"Port {port} in use (not managed by this checkout)"
            return info
        url = _http_url(host, port, _HEALTH_PATH[spec.key])
        info.state = "online" if _http_ok(url, timeout=1.5) else "failed"
        return info

    if spec.kind == "ecommerce":
        built = meta["app"][0] in docker.images
    else:
        built = _built_locally(spec)
    busy = process is not None and _tracked_pid(*process) is not None
    if busy:
        info.state = "failed"
    else:
        info.state = "ready" if built else "missing"
    return info


def _tool_version(command: list[str]) -> str | None:
    try:
        result = _run(command, capture=True, check=False, announce=False, timeout=8)
    except DeployError:
        return None
    if result.returncode != 0:
        return None
    output = (result.stdout or result.stderr).strip()
    match = re.search(r"\d+(?:\.\d+)+", output)
    return match.group(0) if match else ""


_TOOL_COMMANDS = {
    "docker": ["docker", "--version"],
    "docker compose": ["docker", "compose", "version", "--short"],
    "uv": ["uv", "--version"],
    "node": ["node", "--version"],
    "npm": ["npm", "--version"],
}


def _probe_tool(label: str) -> _Item:
    version = _tool_version(_TOOL_COMMANDS[label])
    state, detail = "ready", version or ""
    if version is None:
        state, detail = "failed", "not found"
    elif label == "node" and not (
        version[:1].isdigit() and int(version.split(".")[0]) >= 18
    ):
        state, detail = "failed", f"{version} (needs >= 18)"
    return _Item.dependency(label.replace(" ", "-"), state, label, detail)


def _get_command(item: _Item) -> list[str]:
    """返回确认框展示的原样命令；不执行 shell 或提升权限。"""

    if item.target is None:
        raise DeployError("Select a tool or image to get.")
    if item.target.key == "env-image-app":
        env_file = ENV_FILE if ENV_FILE.exists() else ENV_EXAMPLE
        return [
            "docker",
            "compose",
            "--env-file",
            _rel(env_file),
            "-f",
            _rel(COMPOSE_FILE),
            "build",
            "app",
        ]
    if item.target.key.startswith("env-image-"):
        return ["docker", "image", "pull", item.main]
    if item.main == "docker daemon":
        if sys.platform == "darwin":
            return ["open", "-a", "Docker"]
        if shutil.which("systemctl"):
            return ["systemctl", "--no-ask-password", "start", "docker.service"]
        raise DeployError("Start the Docker daemon manually, then check Environment.")

    managers = (
        ("brew", {"docker compose": "docker-compose", "npm": "node"}),
        (
            "apt-get",
            {
                "docker": "docker.io",
                "docker compose": "docker-compose-v2",
                "node": "nodejs",
            },
        ),
        (
            "dnf",
            {
                "docker": "moby-engine",
                "docker compose": "docker-compose-plugin",
                "node": "nodejs",
            },
        ),
    )
    for manager, packages in managers:
        if shutil.which(manager):
            flags = [] if manager == "brew" else ["-y"]
            return [manager, "install", *flags, packages.get(item.main, item.main)]
    raise DeployError(
        f"No supported package manager (brew/apt-get/dnf). Install {item.main} manually."
    )


def _get_dependency(item: _Item, command: list[str]) -> None:
    _run(command, cwd=ROOT)
    _check_cancel()
    if item.main == "docker daemon":
        # Docker Desktop 窗口打开后，引擎仍可能在启动中。
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            _check_cancel()
            if _docker_info().ok:
                break
            time.sleep(1)
        else:
            raise DeployError(
                "Docker daemon did not become ready; check Docker's status."
            )
    elif item.main in _TOOL_COMMANDS:
        result = _probe_tool(item.main)
        if result.state != "ready":
            raise DeployError(
                f"{item.main}: {result.sub}. Check your PATH or package version."
            )
    elif not _image_exists(item.main):
        raise DeployError(f"Image not found after Get: {item.main}")
    _ok(f"{item.main} ready")


class _Monitor:
    """后台探测线程：定期刷新每行状态、Docker 信息与工具版本。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._tools_wake = threading.Event()
        self._stop = threading.Event()
        self._force = True
        self._reload_meta = True
        self._rows: dict[str, _RowInfo] = {}
        self._docker = _DockerInfo()
        self._meta = dict(_COMPOSE_DEFAULTS)
        self._tools: list[_Item] = []

    def start(self) -> None:
        for target in (self._probe_loop, self._tools_loop):
            threading.Thread(target=target, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._tools_wake.set()

    def refresh(self, reload_meta: bool = False) -> None:
        self._force = True
        self._reload_meta = self._reload_meta or reload_meta
        self._wake.set()
        self._tools_wake.set()

    def snapshot(
        self,
    ) -> tuple[
        dict[str, _RowInfo], _DockerInfo, dict[str, tuple[str, str]], list[_Item]
    ]:
        with self._lock:
            return dict(self._rows), self._docker, self._meta, list(self._tools)

    @staticmethod
    def _probe(
        spec: _Spec, docker: _DockerInfo, meta: dict[str, tuple[str, str]]
    ) -> _RowInfo:
        try:
            return _probe_row(spec, docker, meta)
        except Exception:  # noqa: BLE001
            return _RowInfo("unknown")

    def _probe_loop(self) -> None:
        _silence()
        docker = _DockerInfo()
        meta = self._meta
        docker_at = 0.0
        with ThreadPoolExecutor(len(_SPECS), initializer=_silence) as pool:
            while not self._stop.is_set():
                self._wake.clear()
                if self._reload_meta:
                    self._reload_meta = False
                    meta = _compose_meta(refresh=True)
                if self._force or time.monotonic() - docker_at >= 5.0:
                    self._force = False
                    docker = _docker_info()
                    docker_at = time.monotonic()
                rows = list(
                    pool.map(
                        self._probe,
                        _SPECS,
                        [docker] * len(_SPECS),
                        [meta] * len(_SPECS),
                    )
                )
                with self._lock:
                    self._rows = {s.key: row for s, row in zip(_SPECS, rows)}
                    self._docker = docker
                    self._meta = meta
                self._wake.wait(2.0)

    def _tools_loop(self) -> None:
        _silence()
        while not self._stop.is_set():
            self._tools_wake.clear()
            tools = [_Item("ready", "python", sys.version.split()[0])]
            tools.extend(_probe_tool(label) for label in _TOOL_COMMANDS)
            with self._lock:
                self._tools = tools
            self._tools_wake.wait(10.0)


# ---------------------------------------------------------------------------
# TUI：任务队列
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class _Job:
    spec: _Target
    title: str
    phase: str
    fn: Callable[[], None]
    group: object | None = None
    state: str = "queued"  # queued | running | done | failed | cancelled
    error: str = ""
    tail: deque[str] = field(default_factory=lambda: deque(maxlen=60))
    cancelled: bool = False
    process: subprocess.Popen | None = None


def _make_job(
    action: str,
    spec: _Spec,
    *,
    group: object | None = None,
    force_build: bool = False,
    final: bool = False,
    volumes: bool = False,
) -> _Job:
    if action == "build":
        return _Job(spec, "Build", "Building", lambda: _build_row(spec), group)
    if action == "deploy":
        phase = "Building" if force_build else "Deploying"
        return _Job(
            spec, "Deploy", phase, lambda: _deploy_row(spec, force_build), group
        )
    return _Job(
        spec,
        "Uninstall",
        "Uninstalling",
        lambda: _uninstall_row(spec, final, volumes),
        group,
    )


class _Runner:
    """单 worker 线程串行执行任务；输出写入 var/logs/deploy/<服务>.log。"""

    def __init__(self, on_done: Callable[[], None]) -> None:
        self._cond = threading.Condition()
        self._queue: list[_Job] = []
        self._current: _Job | None = None
        self._failed: dict[str, _Job] = {}
        self._failures: deque[_Job] = deque()
        self._closed = False
        self._on_done = on_done
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def submit(self, jobs: Sequence[_Job]) -> None:
        with self._cond:
            for job in jobs:
                self._failed.pop(job.spec.key, None)
                self._queue.append(job)
            self._cond.notify()

    def has_row(self, key: str) -> bool:
        with self._cond:
            if self._current is not None and self._current.spec.key == key:
                return True
            return any(job.spec.key == key for job in self._queue)

    def view(self) -> dict[str, tuple[str, _Job]]:
        with self._cond:
            result = {key: ("failed", job) for key, job in self._failed.items()}
            for job in self._queue:
                result[job.spec.key] = ("queued", job)
            if self._current is not None:
                result[self._current.spec.key] = ("running", self._current)
            return result

    def counts(self) -> tuple[_Job | None, int]:
        with self._cond:
            return self._current, len(self._queue)

    def busy(self) -> bool:
        with self._cond:
            return self._current is not None or bool(self._queue)

    def pop_failure(self) -> _Job | None:
        with self._cond:
            return self._failures.popleft() if self._failures else None

    def cancel(self, key: str | None = None) -> bool:
        with self._cond:
            pending = [job for job in self._queue if key is None or job.spec.key == key]
            for job in pending:
                job.state = "cancelled"
                job.cancelled = True
                self._queue.remove(job)
            job = self._current
            if job is not None and (key is None or job.spec.key == key):
                job.cancelled = True
                if job.process is not None:
                    _kill_process_group(job.process)
                return True
            return bool(pending)

    def close(self, timeout: float = 5.0) -> None:
        self.cancel()
        with self._cond:
            self._closed = True
            self._cond.notify()
        self._thread.join(timeout)

    def _loop(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                job = self._queue.pop(0)
                job.state = "running"
                self._current = job

            self._execute(job)

            with self._cond:
                self._current = None
                if job.state == "failed":
                    self._failed[job.spec.key] = job
                    self._failures.append(job)
                    if job.group is not None:
                        rest = []
                        for other in self._queue:
                            if other.group is job.group:
                                other.state = "cancelled"
                                other.cancelled = True
                            else:
                                rest.append(other)
                        self._queue = rest
            self._on_done()

    def _execute(self, job: _Job) -> None:
        try:
            DEPLOY_LOG_DIR.mkdir(parents=True, exist_ok=True)
            handle = (DEPLOY_LOG_DIR / f"{job.spec.log_name}.log").open(
                "a", encoding="utf-8", errors="replace"
            )
        except OSError:
            handle = None

        def sink(line: str) -> None:
            job.tail.append(line)
            if handle is not None:
                handle.write(line + "\n")
                handle.flush()

        _CTX.sink = sink
        _CTX.job = job
        try:
            if handle is not None:
                handle.write("\n")
            sink(
                f"===== {time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"{job.title} - {job.spec.name} ====="
            )
            job.fn()
            job.state = "cancelled" if job.cancelled else "done"
        except DeployError as exc:
            if job.cancelled:
                job.state = "cancelled"
            else:
                job.state = "failed"
                job.error = str(exc)
                _error(str(exc))
        except Exception as exc:  # noqa: BLE001
            job.state = "cancelled" if job.cancelled else "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            sink(traceback.format_exc().rstrip())
        finally:
            _CTX.sink = None
            _CTX.job = None
            if handle is not None:
                handle.close()


# ---------------------------------------------------------------------------
# TUI：文本与绘制工具
# ---------------------------------------------------------------------------

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

_SYMBOLS_UNICODE = {
    "tl": "╭",
    "tr": "╮",
    "bl": "╰",
    "br": "╯",
    "h": "─",
    "v": "│",
    "lt": "├",
    "rt": "┤",
    "tt": "┬",
    "bt": "┴",
    "x": "┼",
    "online": "●",
    "ready": "✓",
    "failed": "✗",
    "missing": "-",
    "unknown": "·",
    "queued": "…",
    "ell": "…",
    "spin": "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏",
}
_SYMBOLS_ASCII = {
    "tl": "+",
    "tr": "+",
    "bl": "+",
    "br": "+",
    "h": "-",
    "v": "|",
    "lt": "+",
    "rt": "+",
    "tt": "+",
    "bt": "+",
    "x": "+",
    "online": "*",
    "ready": "v",
    "failed": "x",
    "missing": "-",
    "unknown": ".",
    "queued": ".",
    "ell": "...",
    "spin": "|/-\\",
}
_STATE_COLOR = {
    "online": "green",
    "ready": "green",
    "failed": "red",
    "missing": "gray",
    "unknown": "gray",
}

_MIN_HEIGHT = 20
_MIN_WIDTH = 80


def _clean(text: str) -> str:
    text = _ANSI_RE.sub("", text).replace("\t", "    ")
    return _CONTROL_RE.sub(" ", text)


def _char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 1


def _text_width(text: str) -> int:
    return sum(_char_width(ch) for ch in text)


def _clip_cols(text: str, width: int) -> str:
    used = 0
    out = []
    for ch in text:
        cols = _char_width(ch)
        if used + cols > width:
            break
        out.append(ch)
        used += cols
    return "".join(out)


def _wrap_lines(lines: Sequence[str], width: int) -> list[str]:
    wrapped: list[str] = []
    for line in lines:
        line = _clean(line)
        while _text_width(line) > width > 0:
            piece = _clip_cols(line, width)
            wrapped.append(piece)
            line = line[len(piece) :]
        wrapped.append(line)
    return wrapped


def _max_offset(text: str, width: int, ell: str = "…") -> int:
    """text 在 width 列内显示时，最大的起始字符偏移（尾部恰好放下）。"""

    room = max(0, width - _text_width(ell))
    used = offset = 0
    for i in range(len(text) - 1, -1, -1):
        used += _char_width(text[i])
        if not offset and used > room:
            offset = i + 1
            while offset < len(text) and _char_width(text[offset]) == 0:
                offset += 1
        if used > width:
            return offset
    return 0


def _window(
    text: str, width: int, offset: int = 0, ell: str = "…"
) -> tuple[str, int, int, int]:
    """截取 text 的一个窗口，返回 (显示文本, 修正后的偏移, 头部省略号长度, 尾部省略号长度)。"""

    if width <= 0:
        return "", 0, 0, 0
    max_offset = _max_offset(text, width, ell)
    if not max_offset:
        return text, 0, 0, 0
    offset = max(0, min(offset, max_offset))
    ell_cols = _text_width(ell)
    head = ell if offset > 0 else ""
    tail_reserved = 0 if offset >= max_offset else ell_cols
    room = max(0, width - (ell_cols if head else 0) - tail_reserved)

    body = []
    used = 0
    index = offset
    while index < len(text):
        cols = _char_width(text[index])
        if used + cols > room:
            break
        body.append(text[index])
        used += cols
        index += 1
    tail = ell if index < len(text) else ""
    return head + "".join(body) + tail, offset, len(head), len(tail)


def _fit(
    segs: Sequence[tuple[str, int]],
    width: int,
    offset: int = 0,
    ell_attr: int = 0,
    ell: str = "…",
) -> tuple[list[tuple[str, int]], int]:
    """把带属性的文本片段按 _window 规则裁剪，省略号使用 ell_attr。"""

    text = "".join(piece for piece, _ in segs)
    shown, offset, head, tail = _window(text, width, offset, ell)
    end = offset + len(shown) - head - tail

    out: list[tuple[str, int]] = []
    if head:
        out.append((shown[:head], ell_attr))
    pos = 0
    for piece, attr in segs:
        if pos >= end:
            break
        if pos + len(piece) > offset:
            out.append((piece[max(0, offset - pos) : end - pos], attr))
        pos += len(piece)
    if tail:
        out.append((shown[len(shown) - tail :], ell_attr))
    return out, offset


def _slice_segs(segs: Sequence[tuple[str, int]], start: int) -> list[tuple[str, int]]:
    out = []
    for piece, attr in segs:
        if start >= len(piece):
            start -= len(piece)
            continue
        out.append((piece[start:], attr))
        start = 0
    return out


def _read_log(
    path: Path, max_bytes: int = 512 * 1024, max_lines: int = 5000
) -> list[str]:
    """读取日志文件尾部，去除 ANSI 与控制字符，回车覆盖只保留最后一段。"""

    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
    except OSError:
        return []

    text = data.decode("utf-8", errors="replace")
    if size > max_bytes:
        text = text.split("\n", 1)[-1]
    lines = []
    for raw in text.split("\n"):
        parts = [part for part in raw.split("\r") if part]
        lines.append(_clean(parts[-1] if parts else ""))
    if lines and lines[-1] == "":
        lines.pop()
    return lines[-max_lines:]


@dataclass
class _Modal:
    title: str
    lines: list[str]
    hint: str = ""
    # 键名（小写字符、esc、enter、any）-> 处理函数；按下后弹窗关闭再执行
    keys: dict[str, Callable[[], None]] = field(default_factory=dict)
    danger: bool = False


class _LogView:
    """全屏日志查看器的状态。"""

    def __init__(self, target: _Target) -> None:
        self.target = target
        self.path = DEPLOY_LOG_DIR / f"{target.log_name}.log"
        self.lines: list[str] = []
        self.top = 0
        self.left = 0
        self.follow = True
        self._sig: tuple[int, int] | None = None
        self._checked = 0.0
        self.reload(force=True)

    def reload(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._checked < 0.5:
            return
        self._checked = now
        try:
            stat = self.path.stat()
            sig = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            sig = None
        if force or sig != self._sig:
            self._sig = sig
            self.lines = _read_log(self.path)


# ---------------------------------------------------------------------------
# TUI：界面
# ---------------------------------------------------------------------------

_Option = tuple[str, str, int | None]  # (键名, 描述, 热键下划线位置)


def _key_name(key: int) -> str:
    if key == 27:
        return "esc"
    if key in (10, 13, curses.KEY_ENTER):
        return "enter"
    if 32 <= key < 127:
        return chr(key).lower()
    return ""


def _tail_lines(job: _Job, count: int) -> list[str]:
    try:
        return list(job.tail)[-count:] if count > 0 else []
    except RuntimeError:
        return []


class _Tui:
    """htop 风格的 curses 界面：Status / Config / Environment 三个页签。"""

    TABS = ("Status", "Config", "Environment")
    TAB_KEYS = ("q", "w", "e")
    STATUS, CONFIG, ENVIRONMENT = 0, 1, 2

    def __init__(self, scr, monitor: _Monitor, runner: _Runner) -> None:
        self.scr = scr
        self.monitor = monitor
        self.runner = runner
        self.h = self.w = 0
        self.running = True
        self.tab = self.STATUS
        self.modal: _Modal | None = None
        self.quit_modal: _Modal | None = None
        self.log: _LogView | None = None
        self.flash_text = ""
        self.flash_until = 0.0
        self.flash_error = False

        self.sel = 0
        self.col = 0
        self.manual_offset: int | None = None
        self.focus_span = (0, 0, 0)  # offset, limit, width
        self.deploy_top = 0
        self.marquee_since = time.monotonic()

        self.panel = 0
        self.psel = [0, 0, 0]
        self.env_groups: tuple[list[_Item], ...] = ([], [], [])

        self.cfg_top = 0
        self.cfg_left = 0
        self._cfg_sig: object = ()
        self._cfg_lines: list[str] | None = None

        self._init_terminal()

    # -- 初始化 -----------------------------------------------------------

    def _init_terminal(self) -> None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.scr.keypad(True)
        self.scr.timeout(100)

        encoding = (locale.getpreferredencoding(False) or "").lower()
        self.sym = _SYMBOLS_UNICODE if "utf" in encoding else _SYMBOLS_ASCII

        a = {
            "plain": 0,
            "bold": curses.A_BOLD,
            "rev": curses.A_REVERSE,
            "ul": curses.A_UNDERLINE,
            "yellow": curses.A_BOLD,
            "green": 0,
            "red": curses.A_BOLD,
            "gray": curses.A_DIM,
            "white": curses.A_BOLD,
            "tab_active": curses.A_REVERSE,
        }
        if curses.has_colors():
            try:
                curses.start_color()
                curses.use_default_colors()
                wide = curses.COLORS >= 256
                palette = {
                    "yellow": 220 if wide else curses.COLOR_YELLOW,
                    "green": 41 if wide else curses.COLOR_GREEN,
                    "red": 203 if wide else curses.COLOR_RED,
                    "gray": 245 if wide else None,
                    "white": 231 if wide else curses.COLOR_WHITE,
                }
                for index, (name, color) in enumerate(palette.items(), start=1):
                    if color is not None:
                        curses.init_pair(index, color, -1)
                        a[name] = curses.color_pair(index)
                tab_pair = len(palette) + 1
                curses.init_pair(
                    tab_pair,
                    curses.COLOR_BLACK,
                    231 if wide else curses.COLOR_WHITE,
                )
                a["tab_active"] = curses.color_pair(tab_pair)
            except curses.error:
                pass
        self.a = a

    # -- 绘制原语 ---------------------------------------------------------

    def put(self, y: int, x: int, text: str, attr: int = 0) -> int:
        if not text or y < 0 or y >= self.h or x < 0 or x >= self.w:
            return x
        clipped = _clip_cols(text, self.w - x)
        try:
            self.scr.addstr(y, x, clipped, attr)
        except curses.error:
            pass
        return x + _text_width(clipped)

    def cell(
        self,
        y: int,
        x: int,
        width: int,
        segs: Sequence[tuple[str, int]],
        extra: int = 0,
        *,
        marquee: bool = True,
        offset: int | None = None,
    ) -> tuple[int, int]:
        """在固定宽度内绘制文本。跑马灯在首尾稍作停留，短文本保持静止。"""

        if width <= 0:
            return 0, 0
        text = "".join(piece for piece, _ in segs)
        ell = self.sym["ell"]
        limit = _max_offset(text, width, ell)
        parts = segs
        if limit:
            if offset is None:
                step = (
                    int((time.monotonic() - self.marquee_since) * 4) if marquee else 0
                )
                offset = max(0, step % (limit + 10) - 5)
            offset = min(limit, offset)
            while offset and offset < len(text) and _char_width(text[offset]) == 0:
                offset += 1
            parts, _ = _fit(segs, width, offset, self.a["gray"], ell)
        end = x
        for text, attr in parts:
            end = self.put(y, end, text, attr | extra)
        if extra and end < x + width:
            self.put(y, end, " " * (x + width - end), extra)
        return (offset or 0) if limit else 0, limit

    def box(self, top: int, bottom: int) -> None:
        s = self.sym
        left, right = 1, self.w - 2
        inner = right - left - 1
        self.put(top, left, s["tl"] + s["h"] * inner + s["tr"])
        for y in range(top + 1, bottom):
            self.put(y, left, s["v"])
            self.put(y, right, s["v"])
        self.put(bottom, left, s["bl"] + s["h"] * inner + s["br"])

    def divider(self, y: int, joins: Sequence[int] = ()) -> None:
        s = self.sym
        left, right = 1, self.w - 2
        chars = [s["h"]] * (right - left - 1)
        for x in joins:
            chars[x - left - 1] = s["x"]
        self.put(y, left, s["lt"] + "".join(chars) + s["rt"])

    def _spin(self) -> str:
        frames = self.sym["spin"]
        return frames[int(time.monotonic() * 10) % len(frames)]

    def _log_attr(self, line: str) -> int:
        a = self.a
        if line.startswith("[ fail ]"):
            return a["red"]
        if line.startswith("[ warn ]"):
            return a["yellow"]
        if line.startswith("[  ok  ]"):
            return a["green"]
        if line.startswith("=="):
            return a["yellow"]
        if line.startswith("[deploy]"):
            return a["gray"]
        return 0

    def flash(self, text: str, *, error: bool = False, seconds: float = 3.0) -> None:
        self.flash_text = text
        self.flash_error = error
        self.flash_until = time.monotonic() + seconds

    # -- 通用栏位 ---------------------------------------------------------

    def _draw_option(self, y: int, x: int, item: _Option) -> int:
        key, desc, underline = item
        a = self.a
        if underline is None:
            x = self.put(y, x, key, a["white"])
        else:
            x = self.put(y, x, key[:underline], a["white"])
            x = self.put(y, x, key[underline], a["white"] | a["ul"])
            x = self.put(y, x, key[underline + 1 :], a["white"])
        return self.put(y, x, desc, a["gray"])

    def _draw_options(
        self,
        y: int,
        left: Sequence[_Option],
        right: Sequence[_Option] = (),
        floor: int = 2,
    ) -> None:
        x = 2
        gap = 2
        if left and (
            x
            + sum(_text_width(key) + _text_width(desc) for key, desc, _ in left)
            + gap * (len(left) - 1)
            > self.w
        ):
            gap = 1
        for item in left:
            x = self._draw_option(y, x, item) + gap
        if not right:
            return
        width = sum(_text_width(k) + _text_width(d) for k, d, _ in right)
        width += 3 * (len(right) - 1)
        start = self.w - 2 - width
        if start > max(x, floor):
            for item in right:
                start = self._draw_option(y, start, item) + 3

    def _draw_nav(self) -> None:
        a = self.a
        x = 2
        for index, name in enumerate(self.TABS):
            if index:
                x = self.put(0, x, " ")
            active = index == self.tab
            text_attr = a["tab_active"] if active else a["white"]
            hint_attr = a["tab_active"] if active else a["gray"]
            x = self.put(0, x, name, text_attr)
            x = self.put(0, x, f"({self.TAB_KEYS[index].upper()})", hint_attr)
        right = (
            ("Arrow-keys", ": Navigation", None),
            ("Esc", ": Quit", None),
        )
        self._draw_options(0, (), right, floor=x)

    def _status_options(self) -> list[_Option]:
        """当前可用的 Status 页快捷键；不可用的操作不展示。"""

        spec = _SPECS[self.sel]
        row_ready = not self.runner.has_row(spec.key)
        options: list[_Option] = []
        if row_ready:
            options.extend(
                (
                    ("B", ":Build", None),
                    ("V", ":Deploy", None),
                )
            )
        if any(not self.runner.has_row(key) for key in _DEPLOY_ORDER):
            options.append(("A", ":Deploy All", None))
        if row_ready:
            options.append(("Z", ":Uninstall", None))
        if not any(self.runner.has_row(key) for key in _UNINSTALL_ORDER):
            options.append(("X", ":Uninstall All", None))
        options.append(("L", ":Log", None))
        if self.runner.busy():
            options.append(("K", ":Cancel", None))
        return options

    def _draw_page_options(self) -> None:
        y = self.h - 2
        if self.tab == self.CONFIG:
            self._draw_options(y, (("Enter", ":Edit", None),))
        elif self.tab == self.ENVIRONMENT:
            options: list[_Option] = [
                ("Left / Right", ":Switch Panels", None),
                ("Up / Down", ":Select Items", None),
            ]
            if any(self._pending_get_items()):
                options.append(("A", ":Get All", None))
            item = self._selected_dependency()
            if item is not None and item.target is not None:
                pending = self.runner.has_row(item.target.key)
                if not pending and item.state in {"missing", "failed"}:
                    options.append(("G", ":Get", None))
                options.append(("L", ":Log", None))
                if pending:
                    options.append(("X", ":Cancel", None))
            self._draw_options(y, options)
        else:
            self._draw_options(y, self._status_options())

    def _draw_status_bar(self) -> None:
        y = self.h - 1
        a = self.a
        if self.flash_text and time.monotonic() < self.flash_until:
            attr = a["red"] if self.flash_error else a["yellow"]
            self.cell(y, 2, self.w - 4, [(_clean(self.flash_text), attr)])
            return
        job, queued = self.runner.counts()
        if job is None and not queued:
            self.put(y, 2, "Idle", a["gray"])
            return
        parts = []
        if job is not None:
            parts.extend(
                [(self._spin() + " ", a["yellow"]), (f"{job.phase} {job.spec.name}", 0)]
            )
        if queued:
            parts.append((f"  {queued} queued", a["gray"]))
        self.cell(y, 2, self.w - 4, parts)

    # -- Status 页 --------------------------------------------------------

    def _geometry(self) -> list[tuple[int, int]]:
        iw = self.w - 4
        url_w = max(24, min(30, iw - 54))
        docker_w = max(8, iw - 30 - url_w)
        return [(10, 18), (30, url_w), (32 + url_w, docker_w)]

    def _row_view(
        self, spec: _Spec, info: _RowInfo, state: tuple[str, _Job] | None
    ) -> tuple[str, int, str, int]:
        a, s = self.a, self.sym
        if state is not None:
            kind, job = state
            if kind == "running":
                return self._spin(), a["yellow"], f"{job.phase}...", a["yellow"]
            if kind == "queued":
                return s["queued"], a["gray"], "Queued...", a["gray"]
            return s["failed"], a["red"], f"Failed: {job.error}", a["red"]
        attr = 0 if info.state == "online" else a["gray"]
        icon = a["gray" if info.state == "ready" else _STATE_COLOR[info.state]]
        return s[info.state], icon, _clean(info.url), attr

    def _draw_status_page(self, top: int, bottom: int) -> None:
        a = self.a
        rows, _docker, meta, _tools = self.monitor.snapshot()
        view = self.runner.view()
        geo = self._geometry()

        self.put(top + 1, 2, "Status", a["yellow"])
        for (x, _width), title in zip(geo, ("Service Name", "URL", "Docker Image")):
            self.put(top + 1, x, title, a["yellow"])
        self.divider(top + 2)

        self.sel = max(0, min(self.sel, len(_SPECS) - 1))
        detail_x = geo[1][0]
        detail_width = self.w - 2 - detail_x
        lines = []
        selected_start = selected_end = 0
        for index, spec in enumerate(_SPECS):
            info = rows.get(spec.key, _RowInfo())
            state = view.get(spec.key)
            icon, icon_attr, text, attr = self._row_view(spec, info, state)
            merged = (state[0] if state else info.state) == "failed"
            start = len(lines)
            details = (
                _wrap_lines(text.splitlines() or [""], detail_width)
                if merged
                else [text]
            )
            for n, line in enumerate(details):
                cells = [
                    (geo[0], spec.name if n == 0 else "", 0),
                    ((detail_x, detail_width) if merged else geo[1], line, attr),
                ]
                if not merged:
                    cells.append((geo[2], _docker_text(spec, meta), a["gray"]))
                lines.append((index, icon if n == 0 else "", icon_attr, cells))
            if index == self.sel:
                selected_start, selected_end = start, len(lines)
                self.col = min(self.col, len(cells) - 1)

        height = bottom - top - 3
        self.deploy_top = max(0, min(self.deploy_top, len(lines) - height))
        self.deploy_top = min(
            selected_start, max(self.deploy_top, selected_end - height)
        )
        for y, (index, icon, icon_attr, cells) in enumerate(
            lines[self.deploy_top : self.deploy_top + height], top + 3
        ):
            self.put(y, 4, icon, icon_attr)
            for col, ((x, width), text, attr) in enumerate(cells):
                focused = index == self.sel and col == self.col
                offset, limit = self.cell(
                    y,
                    x,
                    width,
                    [(text, attr)],
                    a["rev"] if focused else 0,
                    offset=self.manual_offset if focused else None,
                )
                if focused:
                    self.focus_span = offset, limit, width

    # -- Environment 页 ---------------------------------------------------

    def _env_items(self) -> tuple[list[_Item], list[_Item], list[_Item]]:
        rows, docker, meta, tools = self.monitor.snapshot()
        ports = []
        for spec in _SPECS:
            try:
                _host, port = _endpoint(spec)
            except DeployError:
                continue
            info = rows.get(spec.key)
            if info is None:
                state = "unknown"
            elif info.state == "online":
                state = "online"
            elif info.listening:
                state = "failed"
            else:
                state = "ready"
            ports.append(_Item(state, str(port), spec.name))
        ports.sort(key=lambda item: int(item.main))

        environment = list(tools)
        if tools and docker.probed:
            daemon = _Item.dependency(
                "docker-daemon",
                "ready" if docker.ok else "failed",
                "docker daemon",
                "running" if docker.ok else "not running",
            )
            environment.insert(3, daemon)

        images = []
        for service in ("app", "postgres", "redis"):
            image = meta[service][0]
            if not docker.probed:
                state = "unknown"
            else:
                state = "ready" if image in docker.images else "missing"
            images.append(_Item.dependency(f"image-{service}", state, image))
        return ports, environment, images

    def _draw_environment(self, top: int, bottom: int) -> None:
        a, s = self.a, self.sym
        self.env_groups = self._env_items()
        jobs = self.runner.view()
        titles = ("port", "environment", "docker image")
        base, extra = divmod(self.w - 6, 3)
        widths = [base + (1 if index < extra else 0) for index in range(3)]
        xs = [2, 3 + widths[0], 4 + widths[0] + widths[1]]
        seps = [xs[1] - 1, xs[2] - 1]

        for sx in seps:
            for y in range(top + 1, bottom):
                self.put(y, sx, s["v"])
            self.put(top, sx, s["tt"])
            self.put(bottom, sx, s["bt"])
        self.divider(top + 2, joins=seps)

        for panel, (title, items) in enumerate(zip(titles, self.env_groups)):
            px, pw = xs[panel], widths[panel]
            active = panel == self.panel
            self.put(
                top + 1,
                px,
                f" {title} ",
                a["yellow"] | (a["rev"] if active else 0),
            )
            self.psel[panel] = max(0, min(self.psel[panel], len(items) - 1))
            if not items:
                self.put(top + 3, px + 1, "Probing...", a["gray"])
                continue
            for index, item in enumerate(items):
                segs = [(item.main, 0)]
                if item.sub:
                    segs.append((" " + item.sub, a["gray"]))
                y = top + 3 + index
                if y >= bottom:
                    continue
                icon, attr = s[item.state], a[_STATE_COLOR[item.state]]
                job = jobs.get(item.target.key) if item.target is not None else None
                if job is not None:
                    if job[0] == "running":
                        icon, attr = self._spin(), a["yellow"]
                    elif job[0] == "queued":
                        icon, attr = s["queued"], a["gray"]
                    elif item.state != "ready":
                        icon, attr = s["failed"], a["red"]
                self.put(y, px + 1, icon, attr)
                focused = active and index == self.psel[panel]
                self.cell(
                    y,
                    px + 3,
                    pw - 4,
                    segs,
                    a["rev"] if focused else 0,
                )

    # -- Config 页 --------------------------------------------------------

    def _config_lines(self) -> list[str] | None:
        try:
            stat = ENV_FILE.stat()
            sig: object = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            sig = None
        if sig != self._cfg_sig:
            self._cfg_sig = sig
            try:
                text = ENV_FILE.read_text(encoding="utf-8", errors="replace")
                self._cfg_lines = [_clean(line) for line in text.splitlines()]
            except OSError:
                self._cfg_lines = None
        return self._cfg_lines

    def _env_segments(self, line: str) -> list[tuple[str, int]]:
        a = self.a
        if line.lstrip().startswith("#"):
            return [(line, a["gray"])]
        key, sep, value = line.partition("=")
        if sep and key.strip():
            return [(key, a["yellow"]), (sep + value, 0)]
        return [(line, 0)]

    def _draw_config(self, top: int, bottom: int) -> None:
        a = self.a
        lines = self._config_lines()
        width = self.w - 4

        self.cell(top + 1, 2, width, [(str(ENV_FILE), a["bold"])])
        self.divider(top + 2)

        if lines is None:
            message = "File not found. Press Enter to create it from .env.example."
            self.put(top + 3, 2, message, a["gray"])
            return

        rows = bottom - top - 3
        self.cfg_top = max(0, min(self.cfg_top, max(0, len(lines) - rows)))
        longest = max((len(line) for line in lines), default=0)
        self.cfg_left = max(0, min(self.cfg_left, max(0, longest - width + 1)))
        for index in range(rows):
            number = self.cfg_top + index
            if number >= len(lines):
                break
            segs = _slice_segs(self._env_segments(lines[number]), self.cfg_left)
            self.cell(top + 3 + index, 2, width, segs, marquee=self.cfg_left == 0)

        counter = f" {min(self.cfg_top + rows, len(lines))}/{len(lines)} "
        self.put(bottom, self.w - 3 - len(counter), counter, a["gray"])

    # -- 弹窗、日志与整体绘制 ---------------------------------------------

    def _draw_modal(self) -> None:
        modal, a, s = self.modal, self.a, self.sym
        if modal is None:
            return
        want = max(
            [_text_width(line) for line in modal.lines]
            + [_text_width(modal.title) + 2, _text_width(modal.hint), 36]
        )
        inner = min(want, min(self.w - 8, 100))
        lines = _wrap_lines(modal.lines, inner)[-max(1, self.h - 9) :]
        width = inner + 4
        height = len(lines) + 5
        x0 = (self.w - width) // 2
        y0 = (self.h - height) // 2

        for y in range(height):
            self.put(y0 + y, x0, " " * width)
        self.put(y0, x0, s["tl"] + s["h"] * (width - 2) + s["tr"])
        for y in range(1, height - 1):
            self.put(y0 + y, x0, s["v"])
            self.put(y0 + y, x0 + width - 1, s["v"])
        self.put(y0 + height - 1, x0, s["bl"] + s["h"] * (width - 2) + s["br"])
        self.cell(
            y0,
            x0 + 2,
            inner,
            [(f" {modal.title} ", a["red" if modal.danger else "yellow"])],
        )
        for index, line in enumerate(lines):
            text = _clip_cols(line, inner)
            self.put(y0 + 2 + index, x0 + 2, text, self._log_attr(line))
        self.put(y0 + height - 2, x0 + 2, _clip_cols(modal.hint, inner), a["gray"])

    def _draw_log(self) -> None:
        view, a = self.log, self.a
        if view is None:
            return
        view.reload()
        rows = self.h - 2
        max_top = max(0, len(view.lines) - rows)
        if view.follow:
            view.top = max_top
        view.top = max(0, min(view.top, max_top))
        longest = max((len(line) for line in view.lines), default=0)
        view.left = max(0, min(view.left, max(0, longest - (self.w - 4) + 1)))

        total = len(view.lines)
        state = "FOLLOW" if view.follow else "PAUSED"
        right = f"{state}  {min(view.top + rows, total)}/{total}"
        x = self.put(0, 2, "Log: ", a["yellow"])
        room = self.w - 4 - len(right) - x
        self.cell(
            0,
            x,
            room,
            [(view.target.name, a["bold"]), ("  " + _rel(view.path), a["gray"])],
        )
        self.put(
            0, self.w - 2 - len(right), right, a["green"] if view.follow else a["gray"]
        )

        if not view.lines:
            self.put(1, 2, "No log yet.", a["gray"])
        for index in range(rows):
            number = view.top + index
            if number >= total:
                break
            line = view.lines[number]
            self.cell(
                1 + index,
                2,
                self.w - 4,
                [(line[view.left :], self._log_attr(line))],
                marquee=view.left == 0,
            )
        self._draw_options(
            self.h - 1,
            (
                ("Arrow-keys", ":Scroll", None),
                ("PgUp/PgDn", "", None),
                ("Home/End", "", None),
                ("F", ":Follow", None),
                ("Esc", ":Back", None),
                *(
                    [("X", ":Cancel", None)]
                    if self.runner.has_row(view.target.key)
                    else []
                ),
            ),
        )

    def _draw_small(self) -> None:
        text = (
            f"Terminal too small ({self.w}x{self.h}); need {_MIN_WIDTH}x{_MIN_HEIGHT}."
        )
        for index, line in enumerate(_wrap_lines([text, "Esc:Quit"], max(10, self.w))):
            self.put(index, 0, line)

    def draw(self) -> None:
        size = self.scr.getmaxyx()
        if size != (self.h, self.w):
            self.h, self.w = size
            self.marquee_since = time.monotonic()
            self.manual_offset = None
        self.scr.erase()
        if self.h < _MIN_HEIGHT or self.w < _MIN_WIDTH:
            self._draw_small()
        else:
            if self.log is not None:
                self._draw_log()
            else:
                top, bottom = 1, self.h - 3
                self._draw_nav()
                self.box(top, bottom)
                pages = (
                    self._draw_status_page,
                    self._draw_config,
                    self._draw_environment,
                )
                pages[self.tab](top, bottom)
                self._draw_page_options()
                self._draw_status_bar()
            self._draw_modal()
        self.scr.refresh()

    # -- 按键处理 ---------------------------------------------------------

    def handle(self, key: int) -> None:
        self.marquee_since = time.monotonic()
        if key == curses.KEY_RESIZE:
            return
        if self.modal is not None:
            self._handle_modal(key)
            return
        name = _key_name(key)
        if self.h < _MIN_HEIGHT or self.w < _MIN_WIDTH:
            if name == "esc":
                self.request_quit()
            return
        if self.log is not None:
            self._handle_log(key, name)
            return
        if name == "esc":
            self.request_quit()
        elif name in self.TAB_KEYS:
            self._switch_tab(self.TAB_KEYS.index(name))
        elif self.tab == self.CONFIG:
            self._handle_config(key, name)
        elif self.tab == self.ENVIRONMENT:
            self._handle_environment(key, name)
        else:
            self._handle_status(key, name)

    def _switch_tab(self, tab: int) -> None:
        self.tab = tab
        self.manual_offset = None

    def _handle_modal(self, key: int) -> None:
        modal = self.modal
        if modal is None:
            return
        name = _key_name(key)
        handler = modal.keys.get(name) if name else None
        if handler is None:
            handler = modal.keys.get("any")
        if handler is None:
            return
        self.modal = None
        self.quit_modal = None
        handler()

    def _handle_config(self, key: int, name: str) -> None:
        page = max(1, self.h - 8)
        if key == curses.KEY_UP:
            self.cfg_top -= 1
        elif key == curses.KEY_DOWN:
            self.cfg_top += 1
        elif key == curses.KEY_PPAGE:
            self.cfg_top -= page
        elif key == curses.KEY_NPAGE:
            self.cfg_top += page
        elif key == curses.KEY_HOME:
            self.cfg_top = 0
        elif key == curses.KEY_END:
            self.cfg_top = 1 << 30
        elif key == curses.KEY_LEFT:
            self.cfg_left = max(0, self.cfg_left - 8)
        elif key == curses.KEY_RIGHT:
            self.cfg_left += 8
        elif name == "enter":
            self._edit_config()
        self.cfg_top = max(0, self.cfg_top)

    def _handle_environment(self, key: int, name: str) -> None:
        if key == curses.KEY_LEFT:
            self.panel = max(0, self.panel - 1)
        elif key == curses.KEY_RIGHT:
            self.panel = min(len(self.env_groups) - 1, self.panel + 1)
        elif key in {curses.KEY_UP, curses.KEY_DOWN, curses.KEY_HOME, curses.KEY_END}:
            last = max(0, len(self.env_groups[self.panel]) - 1)
            current = self.psel[self.panel]
            target = {
                curses.KEY_UP: current - 1,
                curses.KEY_DOWN: current + 1,
                curses.KEY_HOME: 0,
                curses.KEY_END: last,
            }[key]
            self.psel[self.panel] = max(0, min(last, target))
        elif name == "a":
            self._confirm_get_all()
        elif name in {"g", "l", "x"}:
            item = self._selected_dependency()
            if item is None or item.target is None:
                return
            if name == "g":
                self._confirm_get(item)
            elif name == "l":
                self._open_log(item.target)
            else:
                self._cancel(item.target.key)

    def _selected_dependency(self) -> _Item | None:
        items = self.env_groups[self.panel]
        return items[self.psel[self.panel]] if items else None

    def _confirm_get(self, item: _Item) -> None:
        target = item.target
        if target is None or item.state not in {"failed", "missing"}:
            return
        if self.runner.has_row(target.key):
            self.flash(f"{item.main} already has a pending task.")
            return
        try:
            command = _get_command(item)
        except DeployError as exc:
            self.flash(str(exc), error=True, seconds=8)
            return

        def start() -> None:
            self.runner.submit(
                [_Job(target, "Get", "Getting", lambda: _get_dependency(item, command))]
            )

        self.modal = _Modal(
            f"Get: {item.main}",
            ["Run in the project directory:", "", shlex.join(command)],
            "Y: Run   N / Esc: Cancel",
            {"y": start, "n": lambda: None, "esc": lambda: None},
        )

    def _pending_get_items(self) -> tuple[list[_Item], list[_Item]]:
        """Return missing tools and images that do not already have a job."""

        if len(self.env_groups) < 3:
            return [], []
        tools = [
            item
            for item in self.env_groups[1]
            if item.target is not None
            and item.state in {"missing", "failed"}
            and not self.runner.has_row(item.target.key)
        ]
        images = [
            item
            for item in self.env_groups[2]
            if item.target is not None
            and item.state in {"missing", "failed"}
            and not self.runner.has_row(item.target.key)
        ]
        return tools, images

    def _confirm_get_all(self) -> None:
        """Queue Get tasks for every missing dependency and image."""

        tools, images = self._pending_get_items()
        items = [*tools, *images]
        if not items:
            pending = any(
                item.target is not None and self.runner.has_row(item.target.key)
                for group in self.env_groups[1:]
                for item in group
            )
            self.flash(
                "Dependency tasks are already queued."
                if pending
                else "All dependencies are ready."
            )
            return

        plans: list[tuple[_Item, list[str]]] = []
        for item in items:
            try:
                plans.append((item, _get_command(item)))
            except DeployError as exc:
                self.flash(str(exc), error=True, seconds=8)
                return

        def start() -> None:
            group = object()
            jobs: list[_Job] = []
            for item, command in plans:
                target = item.target
                assert target is not None
                jobs.append(
                    _Job(
                        target,
                        "Get",
                        "Getting",
                        lambda item=item, command=command: _get_dependency(
                            item, command
                        ),
                        group,
                    )
                )
            self.runner.submit(jobs)
            self.flash(f"Get All: {len(jobs)} tasks queued.")

        def noop() -> None:
            pass

        summary: list[str] = []
        if tools:
            summary.append("Dependencies: " + ", ".join(item.main for item in tools))
        if images:
            summary.append("Images: " + ", ".join(item.main for item in images))
        self.modal = _Modal(
            "Get All",
            [
                f"{len(tools)} dependencies and {len(images)} images need attention.",
                "",
                *summary,
                "",
                "Tasks run one at a time.",
            ],
            "Y: Run   N / Esc: Cancel",
            {"y": start, "n": noop, "esc": noop},
        )

    def _handle_status(self, key: int, name: str) -> None:
        last = len(_SPECS) - 1
        if key in {curses.KEY_UP, curses.KEY_DOWN, curses.KEY_HOME, curses.KEY_END}:
            target = {
                curses.KEY_UP: self.sel - 1,
                curses.KEY_DOWN: self.sel + 1,
                curses.KEY_HOME: 0,
                curses.KEY_END: last,
            }[key]
            self.sel = max(0, min(last, target))
            self.manual_offset = None
        elif key in {curses.KEY_PPAGE, curses.KEY_NPAGE}:
            self.sel = 0 if key == curses.KEY_PPAGE else last
            self.manual_offset = None
        elif key in {curses.KEY_LEFT, curses.KEY_RIGHT}:
            offset, limit, width = self.focus_span
            direction = 1 if key == curses.KEY_RIGHT else -1
            moved = max(0, min(limit, offset + direction * max(1, width // 2)))
            if moved != offset:
                self.manual_offset = moved
            else:
                self.col = max(0, min(2, self.col + direction))
                self.manual_offset = None
        elif name == "b":
            self._act_row("build")
        elif name == "v":
            self._act_row("deploy")
        elif name == "z":
            self._act_row("uninstall")
        elif name == "a":
            self._deploy_all()
        elif name == "x":
            self._uninstall_all()
        elif name == "l":
            self._open_log(_SPECS[self.sel])
        elif name == "k":
            self._cancel()

    def _handle_log(self, key: int, name: str) -> None:
        view = self.log
        if view is None:
            return
        rows = self.h - 2
        max_top = max(0, len(view.lines) - rows)
        top = max_top if view.follow else view.top
        if name in {"esc", "q", "l"}:
            self.log = None
            return
        if name == "f":
            view.follow = not view.follow
            return
        if name == "x":
            self._cancel(view.target.key)
            return
        if key == curses.KEY_UP:
            top -= 1
        elif key == curses.KEY_DOWN:
            top += 1
        elif key == curses.KEY_PPAGE:
            top -= max(1, rows - 1)
        elif key == curses.KEY_NPAGE:
            top += max(1, rows - 1)
        elif key == curses.KEY_HOME:
            top = 0
        elif key == curses.KEY_END:
            top = max_top
        elif key == curses.KEY_LEFT:
            view.left = max(0, view.left - 8)
            return
        elif key == curses.KEY_RIGHT:
            view.left += 8
            return
        else:
            return
        view.top = max(0, min(top, max_top))
        view.follow = view.top >= max_top

    # -- 动作 -------------------------------------------------------------

    def _act_row(self, action: str) -> None:
        spec = _SPECS[self.sel]
        if self.runner.has_row(spec.key):
            self.flash(f"{spec.name} already has a pending task.")
        else:
            self.runner.submit([_make_job(action, spec)])

    def _deploy_all(self) -> None:
        group = object()
        jobs = [
            _make_job("deploy", _SPEC_BY_KEY[key], group=group, force_build=True)
            for key in _DEPLOY_ORDER
            if not self.runner.has_row(key)
        ]
        if not jobs:
            self.flash("Nothing to deploy: all services already have pending tasks.")
            return
        self.runner.submit(jobs)
        self.flash(f"Deploy All: {len(jobs)} tasks queued.")

    def _uninstall_all(self) -> None:
        if any(self.runner.has_row(key) for key in _UNINSTALL_ORDER):
            self.flash("Some services still have pending tasks. Wait or press K.")
            return

        def start(volumes: bool) -> None:
            group = object()
            jobs = [
                _make_job(
                    "uninstall",
                    _SPEC_BY_KEY[key],
                    group=group,
                    final=key == _UNINSTALL_ORDER[-1],
                    volumes=volumes,
                )
                for key in _UNINSTALL_ORDER
            ]
            self.runner.submit(jobs)
            self.flash(f"Uninstall All: {len(jobs)} tasks queued.")

        def noop() -> None:
            pass

        self.modal = _Modal(
            "Uninstall All",
            [
                "Stop and remove every deployed service.",
                "",
                "Y  Uninstall, keep PostgreSQL / Redis data",
                "V  Uninstall and DELETE data volumes",
                "N  Cancel",
            ],
            "Y / V / N",
            {
                "y": lambda: start(False),
                "v": lambda: start(True),
                "n": noop,
                "esc": noop,
            },
            danger=True,
        )

    def _cancel(self, key: str | None = None) -> None:
        if self.runner.cancel(key):
            self.flash("Cancelling tasks...")
        else:
            self.flash("No running tasks.")

    def _open_log(self, spec: _Target) -> None:
        self.log = _LogView(spec)

    def _show_failure(self, job: _Job) -> None:
        spec = job.spec

        def noop() -> None:
            pass

        self.modal = _Modal(
            f"{job.title} failed: {spec.name}",
            _tail_lines(job, 12) or [job.error],
            "L: Full log   Any other key: Close",
            {"l": lambda: self._open_log(spec), "any": noop},
            danger=True,
        )

    def _edit_config(self) -> None:
        if not ENV_FILE.exists():
            try:
                create_env()
            except (DeployError, OSError) as exc:
                self.flash(f"Cannot create .env: {exc}", error=True)
                return
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        try:
            command = shlex.split(editor)
        except ValueError:
            command = [editor]

        problem = ""
        curses.def_prog_mode()
        curses.endwin()
        try:
            subprocess.call([*command, str(ENV_FILE)])
        except OSError as exc:
            problem = f"Cannot run editor {editor!r}: {exc}"
        except KeyboardInterrupt:
            pass
        finally:
            curses.reset_prog_mode()
            try:
                curses.curs_set(0)
            except curses.error:
                pass
            self.scr.clear()

        load_env(refresh=True)
        self.monitor.refresh(reload_meta=True)
        if problem:
            self.flash(problem, error=True, seconds=6.0)
        else:
            self.flash("Config reloaded.")

    def request_quit(self) -> None:
        if not self.runner.busy():
            self.running = False
            return

        def leave() -> None:
            self.running = False

        def noop() -> None:
            pass

        self.modal = self.quit_modal = _Modal(
            "Quit",
            [
                "Tasks are still running.",
                "Quitting cancels them (background services keep running).",
            ],
            "Y: Quit   N / Esc: Stay",
            {"y": leave, "n": noop, "esc": noop},
            danger=True,
        )

    # -- 主循环 -----------------------------------------------------------

    def _tick(self) -> None:
        if self.modal is None and self.log is None:
            job = self.runner.pop_failure()
            if job is not None:
                self._show_failure(job)

    def run(self) -> None:
        while self.running:
            try:
                self.draw()
                key = self.scr.getch()
                if key != -1:
                    self.handle(key)
                self._tick()
            except KeyboardInterrupt:
                if self.quit_modal is not None and self.modal is self.quit_modal:
                    self.running = False
                else:
                    self.request_quit()


def _tui_main(scr, monitor: _Monitor, runner: _Runner) -> None:
    _Tui(scr, monitor, runner).run()


def _run_tui() -> int:
    """无参数运行时的交互界面入口。"""

    if curses is None:
        _error("当前 Python 不支持 curses（Windows 可安装 windows-curses）。")
        _info("请改用命令行参数，详见 python deploy.py --help")
        return 1

    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    os.environ.setdefault("ESCDELAY", "25")

    _CTX.sink = _discard
    monitor = _Monitor()
    runner = _Runner(on_done=monitor.refresh)
    try:
        load_env(refresh=True)
        monitor.start()
        curses.wrapper(_tui_main, monitor, runner)
    except KeyboardInterrupt:
        pass
    finally:
        _CTX.sink = None
        runner.close()
        monitor.stop()
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deploy.py",
        description="ShopAgent 一键式部署脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "服务名称：ecommerce、customer、ai、frontend、all\n"
            "不带参数运行进入 TUI 交互界面。"
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
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            parser.print_help()
            return 2
        return _run_tui()

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
