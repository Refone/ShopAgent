from typing import Annotated, Any

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
    summary="用户发送消息",
)
async def post_messages(
    payload: ChatMessageRequest,
    chat_service: ChatServiceDep,
    user: Annotated[CurrentUser, Depends(require_roles("user"))],
):

    result = await chat_service.accept_message(user.user_id, payload)

    return result


@chat_router.get(
    "/history",
    summary="获取历史消息",
)
async def get_history(
    chat_service: ChatServiceDep,
    user: Annotated[CurrentUser, Depends(require_roles("user"))],
) -> dict[str, Any]:

    result = await chat_service.get_history(user.user_id)

    return result
