from __future__ import annotations

import uuid

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine

CHAT_TEXT = "我的订单什么时候发货？"


def test_user_frontend_chat_flow(
    api_client: httpx.Client,
    customer_headers: dict[str, str],
    db_engine: Engine,
    chat_user: str,
) -> None:
    """
    看护 user-frontend 聊天主流程：
    1. 进入聊天页时调用 current 接口，接口正常返回；
    2. 在聊天页发送一条消息，message 接口正常响应；
    3. conversations、messages、conversation_turns 均能查询到对应记录。
    """

    # 1. chat 页初始化会先请求 current，读取当前会话状态。
    # current 这里只校验接口响应契约，三张表的落库在发送消息后统一校验。
    current_response = api_client.post(
        "/api/v1/conversations/current",
        headers=customer_headers,
    )
    assert current_response.status_code == 200, current_response.text
    current = current_response.json()
    assert current["mode"] == "AI"
    assert current["is_procession"] is False
    assert isinstance(current["id"], str) and current["id"].startswith("conv_")

    # 2. 聊天页输入消息后发送，message 接口返回会话和模式。
    message_id = f"msg_test_{uuid.uuid4().hex}"
    send_response = api_client.post(
        "/api/v1/chat/messages",
        headers=customer_headers,
        json={
            "message_id": message_id,
            "type": "text",
            "content": {"text": CHAT_TEXT},
        },
    )
    assert send_response.status_code == 200, send_response.text
    accepted = send_response.json()
    assert accepted["mode"] == "AI"
    conversation_id = accepted["conversation_id"]
    assert isinstance(conversation_id, str) and conversation_id.startswith("conv_")

    # 3. 校验数据库落库结果。测试用户是隔离的，因此每个查询都应只有一条记录。
    with db_engine.connect() as connection:
        conversations = (
            connection.execute(
                text(
                    "SELECT id, user_id, mode, input_revision, answered_revision, "
                    "       ended_at "
                    "FROM conversations "
                    "WHERE user_id = :user_id "
                    "ORDER BY last_active_at DESC"
                ),
                {"user_id": chat_user},
            )
            .mappings()
            .all()
        )
        assert len(conversations) == 1, conversations
        conversation = conversations[0]
        assert conversation["id"] == conversation_id
        assert conversation["user_id"] == chat_user
        assert conversation["mode"] == "AI"
        assert conversation["ended_at"] is None
        assert conversation["input_revision"] == 1
        assert conversation["answered_revision"] == 0

        messages = (
            connection.execute(
                text(
                    "SELECT id, message_id, conversation_id, role, message_type, "
                    "       content, input_revision "
                    "FROM messages "
                    "WHERE conversation_id = :conversation_id "
                    "ORDER BY id"
                ),
                {"conversation_id": conversation_id},
            )
            .mappings()
            .all()
        )
        assert len(messages) == 1, messages
        message = messages[0]
        assert message["message_id"] == message_id
        assert message["conversation_id"] == conversation_id
        assert message["role"] == "user"
        assert message["message_type"] == "text"
        assert message["content"] == {"text": CHAT_TEXT}
        assert message["input_revision"] == conversation["input_revision"]

        turns = (
            connection.execute(
                text(
                    "SELECT id, conversation_id, user_id, status, start_revision, "
                    "       snapshot_revision, collect_until, max_collect_until, "
                    "       run_id, locked_by "
                    "FROM conversation_turns "
                    "WHERE conversation_id = :conversation_id "
                    "ORDER BY created_at"
                ),
                {"conversation_id": conversation_id},
            )
            .mappings()
            .all()
        )
        assert len(turns) == 1, turns
        turn = turns[0]
        assert turn["conversation_id"] == conversation_id
        assert turn["user_id"] == chat_user
        assert turn["status"] == "COLLECTING"
        assert turn["start_revision"] == conversation["answered_revision"] + 1
        assert turn["snapshot_revision"] is None
        assert turn["run_id"] is None
        assert turn["locked_by"] is None
        assert turn["collect_until"] <= turn["max_collect_until"]
