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
    Cover the user-frontend chat flow:
    1. Sending a message returns a successful response;
    2. Sending the same message_id again does not create duplicates;
    3. The history endpoint returns the current conversation and messages;
    4. conversations, messages, and conversation_turns all contain the records.
    """

    message_id = f"msg_test_{uuid.uuid4().hex}"

    # 1. Send a message from the chat page.
    send_response = _post_chat_message(api_client, customer_headers, message_id)
    assert send_response.status_code == 200, send_response.text
    assert send_response.json()["message_id"] == message_id

    # 2. Retries/duplicates must not create a second message or turn.
    duplicate_response = _post_chat_message(api_client, customer_headers, message_id)
    assert duplicate_response.status_code == 200, duplicate_response.text

    # 3. Load history when entering the chat page.
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

    # 4. Verify persisted rows. The test user is isolated, so each query returns one row.
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
        assert turn["status"] == "COLLECTING"
        assert turn["start_message_id"] == message["id"]
        assert turn["end_message_id"] == message["id"]
