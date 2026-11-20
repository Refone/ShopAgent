from typing import Annotated

from fastapi import APIRouter, Depends

from customer_service.app.dependencies import ChatServiceDep
from customer_service.infra.auth import CurrentUser, require_roles
from customer_service.schemas.api import ChatMessageRequest

chat_router = APIRouter(
    prefix="/api/v1/chat",
    tags=["会话管理路由: 对话、加载历史消息会话"],
)


@chat_router.post(
    "/messages",
)
async def post_messages(
    payload: ChatMessageRequest,
    chat_service: ChatServiceDep,
    role: Annotated[CurrentUser, Depends(require_roles("user"))],
):

    result = await chat_service.accept_message(role.user_id, payload)

    return result
