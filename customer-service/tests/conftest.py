from __future__ import annotations

import os
import sys
import uuid
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import jwt
import pytest
from jwt import InsecureKeyLengthWarning
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from customer_service.common.settings import Settings, get_settings


def _delete_chat_user_data(engine: Engine, user_id: str) -> None:
    """删除测试用户产生的会话及其从属记录，避免污染开发库。"""
    params = {"user_id": user_id}
    with engine.begin() as connection:
        for table in ("conversation_turns", "messages"):
            connection.execute(
                text(
                    f"DELETE FROM {table} "
                    "WHERE conversation_id IN ("
                    "  SELECT id FROM conversations WHERE user_id = :user_id"
                    ")"
                ),
                params,
            )
        connection.execute(
            text("DELETE FROM realtime_events WHERE target = :user_id"),
            params,
        )
        connection.execute(
            text("DELETE FROM conversations WHERE user_id = :user_id"),
            params,
        )


@pytest.fixture(scope="session")
def settings() -> Settings:
    return get_settings()


@pytest.fixture(scope="session")
def api_base_url(settings: Settings) -> str:
    override = os.getenv("CUSTOMER_SERVICE_API_BASE_URL")
    if override:
        return override.rstrip("/")

    host = settings.customer_service_host
    if host in {"0.0.0.0", "::", ""}:
        host = "127.0.0.1"
    return f"http://{host}:{settings.customer_service_port}"


@pytest.fixture(scope="session")
def api_client(api_base_url: str) -> Iterator[httpx.Client]:
    with httpx.Client(
        base_url=api_base_url,
        timeout=10.0,
        trust_env=False,
    ) as client:
        try:
            client.get("/openapi.json")
        except httpx.HTTPError as exc:
            pytest.fail(
                f"无法连接 customer-service（{api_base_url}），"
                f"请先启动服务并确认端口配置：{exc}"
            )
        yield client


@pytest.fixture(scope="session")
def db_engine(settings: Settings) -> Iterator[Engine]:
    engine = create_engine(settings.postgres_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        engine.dispose()
        pytest.fail(f"无法连接 PostgreSQL（{settings.postgres_url}）：{exc}")

    yield engine
    engine.dispose()


@pytest.fixture
def chat_user(db_engine: Engine) -> Iterator[str]:
    """每个用例使用独立用户，结束后清理其全部会话数据。"""
    user_id = f"test_{uuid.uuid4().hex}"
    _delete_chat_user_data(db_engine, user_id)
    yield user_id
    _delete_chat_user_data(db_engine, user_id)


@pytest.fixture
def customer_headers(settings: Settings, chat_user: str) -> dict[str, str]:
    now = datetime.now(UTC)
    with warnings.catch_warnings():
        # 开发环境 JWT_SECRET 较短，PyJWT 会告警；这里只需要签出测试令牌。
        warnings.simplefilter("ignore", InsecureKeyLengthWarning)
        token = jwt.encode(
            {
                "user_id": chat_user,
                "role": "user",
                "iat": now,
                "exp": now + timedelta(minutes=10),
            },
            settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )
    return {"Authorization": f"Bearer {token}"}
