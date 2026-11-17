from typing import Annotated

from fastapi import APIRouter, Header

from customer_service.app.dependencies import (
    AuthServiceDep,
    ConversationServiceDep,
)
from customer_service.app.schemas.conversation import (
    ConversationDetailResponse,
    CurrentConversationResponse,
)

chat_router = APIRouter(prefix="/api/v1", tags=["聊天会话路由"])


@chat_router.post(
    "/conversations/current",
    response_model=CurrentConversationResponse,
)
async def get_current_conversation(
    conversation_service: ConversationServiceDep,
    auth_service: AuthServiceDep,
    authorization: Annotated[str | None, Header()] = None,
):
    """
    权限限制：
    1. 对应的用户信息
    2. 用户身份角色是否是接口允许的角色("customer")
    """

    authorized_user = auth_service.get_authorized_user(authorization, "customer")

    result = await conversation_service.get_current_conversation(
        authorized_user.user_id
    )

    return result


@chat_router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetailResponse,
)
async def get_conversation_detail(
    conversation_id: str,
    conversation_service: ConversationServiceDep,
    auth_service: AuthServiceDep,
    authorization: Annotated[str | None, Header()] = None,
):
    """
    返回客服工作台需要的完整会话详情
    """

    authorized_user = auth_service.get_authorized_user(authorization, "agent", "admin")

    if not authorized_user:
        raise ValueError("未授权的用户")

    conversation_detail = await conversation_service.get_conversation_detail(
        conversation_id,
    )

    return conversation_detail
