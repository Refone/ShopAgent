from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from customer_service.app.services.auth import AuthService
from customer_service.app.services.conversation import ConversationService
from customer_service.infra.db import get_db_session


def get_auth_service():
    return AuthService()


def get_conversation_service(
    db_session: Annotated[AsyncSession, Depends(get_db_session)],
):
    return ConversationService(db_session)


ConversationServiceDep = Annotated[
    ConversationService, Depends(get_conversation_service)
]

AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
