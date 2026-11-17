from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class ConversationMode(str, Enum):
    AI = "AI"  # AI 客服
    QUEUED = "QUEUED"  # 等待人工
    HUMAN = "HUMAN"  # 人工服务
    CLOSED = "CLOSED"  # 已结束


class CurrentConversationResponse(BaseModel):
    id: str = Field(..., description="标识当前会话, 用于筛选该会话的历史消息")
    mode: ConversationMode = Field(..., description="当前会话的模式")
    is_procession: bool = Field(..., description="AI 正在处理")


class ConversationMessageResponse(BaseModel):
    message_id: str = Field(..., description="消息 ID")
    role: Literal["user", "ai", "human"] = Field(..., description="消息的角色")
    content: dict[str, Any] = Field(..., description="消息内容")
    created_at: datetime = Field(..., description="消息创建时间")


class ConversationDetailResponse(BaseModel):
    messages: list[ConversationMessageResponse] = Field(..., description="会话消息列表")
