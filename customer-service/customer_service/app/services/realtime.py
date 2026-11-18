from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from customer_service.common.utils import get_uid
from customer_service.models.conversation import RealtimeOutbox


class RealTimeOutBoxService:
    def __init__(self, session: AsyncSession):
        self._session = session

    def add_realtime_outbox(
        self,
        event_channel: str,
        event_type: str,
        event_data: dict[str, Any],
        conversation_id: str,
        message_id: str | None,
    ):
        outbox = RealtimeOutbox(
            channel=event_channel,
            event_type=event_type,
            conversation_id=conversation_id,
            data=event_data,
            request_message_id=message_id or get_uid(conversation_id),
        )
        self._session.add(outbox)
