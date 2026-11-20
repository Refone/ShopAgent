from typing import Any

from pydantic import BaseModel, Field

from customer_service.schemas.enums import MessageType


class ChatMessageRequest(BaseModel):
    message_id: str = Field(
        title="消息 ID",
        description="前端创建,为避免由于重试等情况,同一条消息重复发送",
    )
    type: MessageType = Field(
        title="消息类型",
        description="消息的类型，文本 | 消息卡片",
    )
    content: dict[str, Any] = Field(
        title="消息内容",
        description="消息的具体内容，文本消息为文本，消息卡片为结构化数据",
    )
