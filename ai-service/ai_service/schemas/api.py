from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class MessageContent(BaseModel):
    role: str
    type: str
    content: dict[str, Any]


class ReplyType(StrEnum):
    """Reply type returned by the AI service.

    Attributes:
        ANSWER: Normal answer.
        REQUEST_HANDOFF: Request to hand off to a human agent.
        CLARIFY: Clarification needed (missing order id or required information).
        DECLINE: Declined because the request is out of scope.
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
        min_length=1, description="User messages to process in this turn."
    )


class AgentRunResponse(BaseModel):
    status: str = Field(description="Response status.")
    revision: int = Field(description="Turn revision, used to detect stale results.")
    reply_type: ReplyType = Field(description="Reply type, see the ReplyType enum.")
    reply_text: str = Field(description="Reply text that can be shown to the user.")

    handoff_summary: str | None = Field(
        default=None,
        description="Handoff summary, only set for REQUEST_HANDOFF replies.",
    )
    page_action: str | None = Field(
        default=None,
        description="Suggested page URL for a database write operation.",
    )
