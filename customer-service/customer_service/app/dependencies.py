from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from customer_service.app.services.chat import ChatService
from customer_service.app.services.realtime import RealtimeService
from customer_service.infra.pgsql import get_db_session

DBSessionDep = Annotated[AsyncSession, Depends(get_db_session)]


def get_chat_service(
    db_session: DBSessionDep,
    realtime_service: RealtimeServiceDep,
) -> ChatService:
    return ChatService(
        db_session,
        realtime_service,
    )


ChatServiceDep = Annotated[ChatService, Depends(get_chat_service)]


def get_realtime_service(
    db_session: DBSessionDep,
) -> RealtimeService:
    return RealtimeService(db_session)


RealtimeServiceDep = Annotated[RealtimeService, Depends(get_realtime_service)]
