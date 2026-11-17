from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from customer_service.app.repositories.conversation import ConversationRepository
from customer_service.app.repositories.message import MessageRepository
from customer_service.app.repositories.turn import ConversationTurnRepository
from customer_service.app.schemas.conversation import ConversationMode
from customer_service.common.config import get_settings
from customer_service.models.conversation import Conversation, Message


class ConversationService:
    def __init__(self, session: AsyncSession):
        settings = get_settings()
        self._session = session
        self._conversation_repo = ConversationRepository(session)
        self._turn_repo = ConversationTurnRepository(session)
        self._message_repo = MessageRepository(session)

        self._conversation_timeout_seconds = settings.conversation_idle_timeout_seconds

    async def get_current_conversation(self, user_id: str) -> dict[str, Any]:
        """
        获取当前用户的会话信息，如果不存在则创建一个新的会话。
        """

        # 1. 确保当前用户会话要存在
        conversation = await self.ensure_active_conversation(user_id)

        # 2. 当前是否有轮次消息
        is_procession = await self._turn_repo.has_active_turn(conversation.id)

        # repo 层做 flush, service 层负责 commit
        await self._session.commit()

        # 3. 返回当前会话信息
        return {
            "id": conversation.id,
            "mode": conversation.mode,
            "is_procession": is_procession,  # AI 服务有没有在处理轮次消息
        }

    async def ensure_active_conversation(self, user_id: str) -> Conversation:
        """
        1. 当前会话如果已经超时过期，状态改为关闭。
        2. 获得当前有效会话，或是创建一个新会话。
        """
        # 修改已经超时过期的会话
        await self._close_timeout_conversation(user_id)

        # 查询当前用户是否存在一个有效的会话
        conversation = await self._conversation_repo.latest_active_conversation(
            user_id,
            (
                ConversationMode.AI,
                ConversationMode.QUEUED,
                ConversationMode.HUMAN,
            ),
        )

        # 如果查到，直接返回
        if conversation:
            return conversation

        # 如果没有，创建一个智能客服接管的会话 (AI 会话)
        return await self._create_ai_conversation(user_id)

    async def _create_ai_conversation(self, user_id: str) -> Conversation:
        return await self._conversation_repo.add_conversation(
            user_id,
            ConversationMode.AI,
        )

    async def _close_timeout_conversation(self, user_id: str) -> None:
        # 查询当前用户会话是 AI 模式的会话
        conversations: list[
            Conversation
        ] = await self._conversation_repo.find_conversations(
            user_id,
            ConversationMode.AI,
        )

        for conversation in conversations:
            if conversation.is_timeout(
                timeout_seconds=self._conversation_timeout_seconds
            ):
                await self._conversation_repo.close_conversation(conversation)

    async def get_conversation_detail(
        self,
        conversation_id: str,
    ) -> dict[str, list[dict[str, Any]]]:
        """
        获取用户会话下的消息内容
        """

        conversation = await self._conversation_repo.get_conversation_by_id(
            conversation_id
        )
        if not conversation:
            raise ValueError("会话不存在")

        messages = await self._message_repo.get_conversation_messages(conversation_id)

        return {"messages": [self.parse_message(message) for message in messages]}

    def parse_message(self, message: Message) -> dict[str, Any]:

        return {
            "message_id": message.id,
            "role": message.role,
            "content": message.content,
            "created_at": message.created_at,
        }
