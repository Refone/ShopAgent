from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from .conversation import ConversationMode


class MessageType(StrEnum):
    TEXT = "text"
    OBJECT = "object"


class MessageRole(StrEnum):
    USER = "user"
    AI = "ai"
    HUMAN = "human"


class ChatMessageRequest(BaseModel):
    message_id: str = Field(min_length=4, max_length=64, description="消息 ID")
    type: MessageType = Field(..., description="消息类型")
    content: dict[str, Any] = Field(..., description="消息内容")


class AcceptUserMessageResponse(BaseModel):
    """用户消息受理结果"""

    conversation_id: str = Field(..., description="会话 ID")
    mode: ConversationMode = Field(..., description="会话模式")
