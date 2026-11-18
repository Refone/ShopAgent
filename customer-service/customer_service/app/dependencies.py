from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from customer_service.app.services.auth import AuthService
from customer_service.app.services.conversation import ConversationService
from customer_service.app.services.message import MessageService
from customer_service.app.services.realtime import RealTimeOutBoxService
from customer_service.app.services.turn import TurnService
from customer_service.infra.db import get_db_session


def get_auth_service():
    return AuthService()


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


def get_conversation_service(
    db_session: Annotated[AsyncSession, Depends(get_db_session)],
):
    return ConversationService(db_session)


ConversationServiceDep = Annotated[
    ConversationService, Depends(get_conversation_service)
]


def get_turn_service(
    db_session: Annotated[AsyncSession, Depends(get_db_session)],
):
    return TurnService(db_session)


TurnServiceDep = Annotated[TurnService, Depends(get_turn_service)]


def get_realtime_outbox_service(
    db_session: Annotated[AsyncSession, Depends(get_db_session)],
):
    return RealTimeOutBoxService(db_session)


RealTimeOutBoxServiceDep = Annotated[
    RealTimeOutBoxService, Depends(get_realtime_outbox_service)
]


def get_message_service(
    db_session: Annotated[AsyncSession, Depends(get_db_session)],
    conversation_service: ConversationServiceDep,
    turn_service: TurnServiceDep,
    realtime_service: RealTimeOutBoxServiceDep,
):
    return MessageService(
        session=db_session,
        conversation_service=conversation_service,
        outbox_service=realtime_service,
        turn_service=turn_service,
    )


MessageServiceDep = Annotated[MessageService, Depends(get_message_service)]
