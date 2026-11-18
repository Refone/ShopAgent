from typing import Annotated

from fastapi import APIRouter, Header

from customer_service.app.dependencies import AuthServiceDep, MessageServiceDep
from customer_service.app.schemas.message import (
    AcceptUserMessageResponse,
    ChatMessageRequest,
)

message_router = APIRouter(prefix="/api/v1", tags=["聊天路由"])


@message_router.post(
    "/chat/messages",
    response_model=AcceptUserMessageResponse,
)
async def accept_user_message(
    chat_message: ChatMessageRequest,
    auth_service: AuthServiceDep,
    message_service: MessageServiceDep,
    authorization: Annotated[str | None, Header()] = None,
):
    authorized_user = auth_service.get_authorized_user(authorization, "customer")

    result = await message_service.accept_user_message(
        chat_message=chat_message,
        user_id=authorized_user.user_id,
    )
    return result
