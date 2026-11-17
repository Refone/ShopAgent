from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from customer_service.models.conversation import Message


class MessageRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_conversation_messages(self, conversation_id: str) -> list[Message]:
        result = await self._session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.id.desc())
        )

        return list(result.all())
