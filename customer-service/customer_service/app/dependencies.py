from typing import Annotated

from fastapi import Depends

from customer_service.app.services.auth import AuthService
from customer_service.app.services.conversation import ConversationService


def get_auth_service():
    return AuthService()


def get_conversation_service():
    return ConversationService()


ConversationServiceDep = Annotated[
    ConversationService, Depends(get_conversation_service)
]

AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
