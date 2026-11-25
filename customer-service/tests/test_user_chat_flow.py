from __future__ import annotations

import uuid

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine

CHAT_TEXT = "我的订单什么时候发货？"


def _post_chat_message(
    api_client: httpx.Client,
    headers: dict[str, str],
    message_id: str,
) -> httpx.Response:
    return api_client.post(
        "/api/v1/chat/messages",
        headers=headers,
        json={
            "message_id": message_id,
            "type": "text",
            "content": {"text": CHAT_TEXT},
        },
    )


def test_user_frontend_chat_flow(
    api_client: httpx.Client,
    customer_headers: dict[str, str],
    db_engine: Engine,
    chat_user: str,
) -> None:
    """
    看护 user-frontend 聊天主流程：
    1. 发送一条消息，接口正常响应；
    2. 同一 message_id 重复发送不重复落库；
    3. history 接口返回当前会话与历史消息；
    4. conversations、messages、conversation_turns 均能查询到对应记录。
    """

    message_id = f"msg_test_{uuid.uuid4().hex}"

    # 1. 聊天页输入消息后发送。
    send_response = _post_chat_message(api_client, customer_headers, message_id)
    assert send_response.status_code == 200, send_response.text
    assert send_response.json()["message_id"] == message_id

    # 2. 重试/重复提交不应产生第二条消息或第二个轮次。
    duplicate_response = _post_chat_message(api_client, customer_headers, message_id)
    assert duplicate_response.status_code == 200, duplicate_response.text

    # 3. 进入聊天页时加载历史消息。
    history_response = api_client.get(
        "/api/v1/chat/history",
        headers=customer_headers,
    )
    assert history_response.status_code == 200, history_response.text
    history = history_response.json()
    conversation_id = history["conversation_id"]
    assert isinstance(conversation_id, str) and conversation_id.startswith("conv_")
    assert history["conversation_mode"] == "AI"
    assert len(history["messages"]) == 1, history["messages"]
    history_message = history["messages"][0]
    assert history_message["conversation_id"] == conversation_id
    assert history_message["uuid"] == message_id
    assert history_message["role"] == "USER"
    assert history_message["content"] == {"text": CHAT_TEXT}

    # 4. 校验数据库落库结果。测试用户是隔离的，因此每个查询都应只有一条记录。
    with db_engine.connect() as connection:
        conversations = (
            connection.execute(
                text(
                    "SELECT id, uuid, user_id, mode FROM conversations "
                    "WHERE user_id = :user_id"
                ),
                {"user_id": chat_user},
            )
            .mappings()
            .all()
        )
        assert len(conversations) == 1, conversations
        conversation = conversations[0]
        assert conversation["uuid"] == conversation_id
        assert conversation["user_id"] == chat_user
        assert conversation["mode"] == "AI"

        messages = (
            connection.execute(
                text(
                    "SELECT id, uuid, conversation_id, role, message_type, content "
                    "FROM messages WHERE conversation_id = :conversation_id"
                ),
                {"conversation_id": conversation["id"]},
            )
            .mappings()
            .all()
        )
        assert len(messages) == 1, messages
        message = messages[0]
        assert message["uuid"] == history_message["uuid"]
        assert message["uuid"] == message_id
        assert message["conversation_id"] == conversation["id"]
        assert message["role"] == "USER"
        assert message["message_type"] == "text"
        assert message["content"] == {"text": CHAT_TEXT}

        turns = (
            connection.execute(
                text(
                    "SELECT conversation_id, status, start_message_id, end_message_id "
                    "FROM conversation_turns WHERE conversation_id = :conversation_id"
                ),
                {"conversation_id": conversation["id"]},
            )
            .mappings()
            .all()
        )
        assert len(turns) == 1, turns
        turn = turns[0]
        assert turn["conversation_id"] == conversation["id"]
        assert turn["status"] == "OPEN"
        assert turn["start_message_id"] == message["id"]
        assert turn["end_message_id"] == message["id"]
