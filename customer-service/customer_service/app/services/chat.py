from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from customer_service.app.services.realtime import RealtimeService
from customer_service.common.settings import get_settings
from customer_service.common.utils import get_utcnow
from customer_service.infra.logger import LOG
from customer_service.schemas.api import ChatMessageRequest
from customer_service.schemas.entities import Conversation, ConversationTurn, Message
from customer_service.schemas.enums import ConversationMode, MessageRole, TurnStatus


class ChatService:
    def __init__(self, db_session: AsyncSession, realtime_service: RealtimeService):
        self._settings = get_settings()
        self._db = db_session
        self._realtime_service = realtime_service

    async def accept_message(
        self,
        user_id: str,
        payload: ChatMessageRequest,
    ):
        """
        接受前端发送的消息并进行处理
        1. 获取会话
        2. 创建消息实体
        3. 消息处理逻辑根据会话模式做分流
        4. db session commit
        """

        LOG.info("Accepting message from user_id=%s with payload=%s", user_id, payload)

        # 重复提交防御
        result = await self._db.execute(
            select(Message).where(Message.message_id == payload.message_id)
        )
        existing_message = result.scalar_one_or_none()
        if existing_message:
            LOG.warning("Message already exists: %s", existing_message)
            return existing_message

        # 获取会话
        conversation = await self._ensure_conversation(user_id)
        LOG.info("Ensured conversation %s", conversation)

        # 创建消息实体
        message = Message(
            message_id=payload.message_id,
            conversation_id=conversation.id,
            role=MessageRole.USER,
            message_type=payload.type,
            content=payload.content,
        )

        self._db.add(message)
        conversation.update_last_active()

        # 根据会话模式分流处理
        match conversation.mode:
            case "AI":
                # AI 模式处理逻辑
                await self._ensure_turn(conversation.id)
            case "QUEUED" | "HUMAN":
                # 人工模式处理逻辑
                pass
            case _:
                # 其他模式处理逻辑
                pass

        await self._db.commit()
        LOG.info("Committed message %s to the database", message)

        return payload

    # FIXME: 感觉确认会话的逻辑应该放到前端，并且调用接口前就该搞清楚 conversation id
    async def _ensure_conversation(self, user_id: str) -> Conversation:
        """
        确保会话存在，如果不存在则创建
        """

        db_query = await self._db.execute(
            select(Conversation)
            .where(
                Conversation.user_id == user_id,
                Conversation.mode != ConversationMode.CLOSED,
            )
            .order_by(Conversation.last_active_at.desc())
            .with_for_update(of=Conversation)  # 锁定会话行，防止并发创建重复会话
        )

        conversation = db_query.scalar_one_or_none()

        if conversation:
            if conversation.mode == ConversationMode.AI and conversation.is_expired():
                conversation.close()
            else:
                return conversation

        # 如果没有找到合适的会话，则新建一个会话
        conversation = Conversation(
            user_id=user_id,
            mode=ConversationMode.AI,
        )

        self._db.add(conversation)
        await self._db.flush()  # 因为需要用到 conversation 的 id

        return conversation

    async def _ensure_turn(self, conversation_id: int) -> ConversationTurn:
        """
        确保会话轮次存在，如果不存在则创建
        """

        # 先查询是否存在正在 COLLECTING 状态的轮次
        db_query = await self._db.execute(
            select(ConversationTurn)
            .where(
                ConversationTurn.conversation_id == conversation_id,
                ConversationTurn.status == TurnStatus.COLLECTING,
            )
            .with_for_update(of=ConversationTurn)
        )

        turn = db_query.scalar_one_or_none()

        if turn:
            return turn

        # 如果没有找到正在 COLLECTING 状态的轮次，则新建一个轮次
        turn = ConversationTurn(
            conversation_id=conversation_id,
            status=TurnStatus.COLLECTING,
            collect_until=get_utcnow()
            + timedelta(milliseconds=self._settings.message_merge_delay_ms),
        )

        self._db.add(turn)
        await self._db.flush()  # 因为需要用到 turn 的 id

        return turn
