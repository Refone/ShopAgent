from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from customer_service.models.conversation import Conversation, Message


class MessageRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_conversation_messages(self, conversation_id: str) -> list[Message]:
        """
        根据会话 ID 查询该会话下的所有消息，按消息 ID 降序排列
        """
        result = await self._session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.id.desc())
        )

        return list(result.all())

    async def find_conversation_by_message_id(
        self,
        message_id: str,
    ) -> Conversation | None:
        """
        根据消息 ID 查询对应的会话，如果存在则返回该会话，否则返回 None
        """

        result = await self._session.execute(
            select(Conversation)
            .join(Message, Message.conversation_id == Conversation.id)
            .where(Message.message_id == message_id)
        )
        return result.scalar_one_or_none()

    def add_message(self, message: Message):
        """
        添加一条消息到数据库
        """
        self._session.add(message)
