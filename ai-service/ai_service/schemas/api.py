from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class MessageContent(BaseModel):
    role: str
    type: str
    content: dict[str, Any]


class ReplyType(StrEnum):
    """
    回复类型

    Attributes:
        ANSWER: 正常回复
        REQUEST_HANDOFF: 转接人工请求
        CLARIFY: 澄清（没有明确订单号，缺乏必要信息）
        DECLINE: 拒绝（业务无关）
    """

    ANSWER = "ANSWER"
    REQUEST_HANDOFF = "REQUEST_HANDOFF"
    CLARIFY = "CLARIFY"
    DECLINE = "DECLINE"


class AgentRunRequest(BaseModel):
    conversation_id: int
    turn_id: int
    turn_revision: int
    history: list[MessageContent]
    messages: list[MessageContent] = Field(
        min_length=1, description="本轮次需要处理的用户消息"
    )


class AgentRunResponse(BaseModel):
    status: str = Field(description="响应状态")
    revision: int = Field(description="会话轮次修订号, 判断轮次是否过期")
    reply_type: ReplyType = Field(description="回复类型, 见 ReplyType 枚举")
    reply_text: str = Field(description="可直接展示给用户的回复文本")

    handoff_summary: str | None = Field(
        default=None,
        description="仅当 reply_type 为 REQUEST_HANDOFF 时，提供转接人工摘要",
    )
    page_action: str | None = Field(
        default=None, description="仅当 AI 建议进行数据库写操作时，提供页面跳转链接"
    )
