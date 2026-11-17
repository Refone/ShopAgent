from typing import Annotated

from fastapi import APIRouter, Header

from customer_service.app.dependencies import (
    AuthServiceDep,
    ConversationServiceDep,
)

chat_router = APIRouter(prefix="/api/v1", tags=["聊天会话路由"])


@chat_router.post("/conversations/current")
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
