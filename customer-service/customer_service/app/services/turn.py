from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from customer_service.app.repositories.turn import ConversationTurnRepository
from customer_service.common.config import get_settings
from customer_service.common.utils import get_utcnow
from customer_service.models.conversation import Conversation, ConversationTurn, Message


class TurnService:
    def __init__(self, session: AsyncSession):
        self._settings = get_settings()
        self._session = session
        self._turn_repo = ConversationTurnRepository(self._session)

    async def add_message_to_turn(
        self,
        message: Message,
        conversation: Conversation,
    ) -> ConversationTurn:
        """
        将消息保存到 turn 中
        """
        conversation.input_revision += 1
        message.input_revision = conversation.input_revision

        # 查询正在收集状态的会话轮次
        turn = await self._turn_repo.find_collecting_turn(conversation.id)

        # 查询到
        if turn:
            now = get_utcnow()
            delay = timedelta(milliseconds=self._settings.message_merge_delay_ms)

            # 继续延长收集时间 FIXME: 有点没对
            turn.collect_until = min(
                now + delay,
                turn.max_collect_until,
            )

            return turn

        # 没有查询到
        now = get_utcnow()
        turn = ConversationTurn(
            conversation_id=conversation.id,
            user_id=conversation.user_id,
            status="COLLECTING",
            collect_until=now
            + timedelta(milliseconds=self._settings.message_merge_delay_ms),
            max_collect_until=now
            + timedelta(milliseconds=self._settings.message_merge_max_wait_ms),
            started_at=now,
            start_revision=conversation.answered_revision + 1,
        )

        self._turn_repo.add_turn(turn)

        return turn
