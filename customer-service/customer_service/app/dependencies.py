from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from customer_service.app.services.chat import ChatService
from customer_service.infra.pgsql import get_db_session

DBSessionDep = Annotated[AsyncSession, Depends(get_db_session)]


def get_chat_service(
    db_session: DBSessionDep,
) -> ChatService:
    return ChatService(db_session)


ChatServiceDep = Annotated[ChatService, Depends(get_chat_service)]
