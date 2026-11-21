from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from customer_service.app.services.realtime import RealtimeService
from customer_service.common.settings import get_settings
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

        LOG.info(f"Accepting message from user_id={user_id} with payload={payload}")

        # 重复提交防御
        result = await self._db.execute(
            select(Message).where(Message.uuid == payload.message_id)
        )
        existing_message = result.scalar_one_or_none()
        if existing_message:
            LOG.warning(f"Message already exists: {existing_message.uuid}")
            return payload

        # 获取会话
        conversation = await self._ensure_conversation(user_id)
        LOG.info(f"Ensured conversation {conversation.uuid}")

        # 创建消息实体
        new_message = Message(
            uuid=payload.message_id,
            conversation_id=conversation.id,
            role=MessageRole.USER,
            message_type=payload.type,
            content=payload.content,
        )

        self._db.add(new_message)
        await self._db.flush()  # 因为需要用到 new_message 的 id

        conversation.update_last_active()

        # 根据会话模式分流处理
        match conversation.mode:
            case "AI":
                # AI 模式处理逻辑
                turn = await self._get_alive_turn(
                    conversation
                ) or await self._create_new_turn(
                    conversation,
                    new_message,
                )
                LOG.info(
                    f"Ensured turn {turn.id} for conversation[{conversation.mode}] {conversation.id}"
                )
            case "QUEUED" | "HUMAN":
                # 人工模式处理逻辑
                pass
            case _:
                # 其他模式处理逻辑
                pass

        await self._db.commit()
        LOG.info(f"Committed message {new_message} to the database")

        return payload

    # FIXME: 感觉确认会话的逻辑应该放到前端，并且调用接口前就该搞清楚 conversation id
    async def _ensure_conversation(self, user_id: str) -> Conversation:
        """
        确保会话存在，如果不存在则创建
        """

        result = await self._db.execute(
            select(Conversation)
            .where(
                Conversation.user_id == user_id,
                Conversation.mode.in_(ConversationMode.ALIVE),
            )
            .with_for_update(of=Conversation)  # 锁定会话行，防止并发创建重复会话
        )

        conversation = result.scalar_one_or_none()

        if conversation:
            # 当前用户存在未关闭的会话
            if conversation.is_expired():
                LOG.info(
                    f"Conversation[{conversation.mode}] {conversation.id} is expired, closing it"
                )
                conversation.close()
                await self._db.flush()
            else:
                # 如果有 Human 模式或未超时的 AI 模式会话，则直接返回该会话
                LOG.info(
                    f"Found active conversation[{conversation.mode}] {conversation.id} for user_id={user_id}"
                )
                return conversation

        LOG.info(
            f"No active conversation found for user_id={user_id}, creating a new one"
        )
        # 创建新会话
        conversation = Conversation(
            user_id=user_id,
            mode=ConversationMode.AI,
        )
        self._db.add(conversation)
        await self._db.flush()  # new conversation.id required

        return conversation

    async def _get_alive_turn(
        self, conversation: Conversation
    ) -> ConversationTurn | None:
        """
        获取会话中处于活动状态的轮次，如果没有则返回 None
        """

        result = await self._db.execute(
            select(ConversationTurn)
            .where(
                ConversationTurn.conversation_id == conversation.id,
                ConversationTurn.status.in_(TurnStatus.ALIVE),
            )
            .with_for_update(of=ConversationTurn)
        )

        turn = result.scalar_one_or_none()

        return turn

    async def _create_new_turn(
        self,
        conversation: Conversation,
        first_message: Message,
    ) -> ConversationTurn:
        """
        创建一个新的会话轮次，并将第一条消息关联到该轮次
        """

        turn = ConversationTurn(
            conversation_id=conversation.id,
            status=TurnStatus.COLLECTING,
            start_message_id=first_message.id,
            end_message_id=first_message.id,
        )

        self._db.add(turn)
        await self._db.flush()  # 因为需要用到 turn 的 id

        return turn

    async def get_history(self, user_id: str) -> dict[str, Any]:
        """
        获取用户的历史消息，包括活跃会话、关闭会话
        """

        LOG.info(f"Get History from user_id={user_id}")

        # 查询用户的所有消息
        result = await self._db.execute(
            select(
                Message.uuid,
                Conversation.uuid.label("conversation_id"),  # 对外 uuid
                Message.role,
                Message.content,
                Message.created_at,
            )
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(Conversation.user_id == user_id)
        )

        all_messages = [dict(row) for row in result.mappings().all()]

        conversation = await self._ensure_conversation(user_id)

        await self._db.commit()

        LOG.info(
            f"Fetched {len(all_messages)} messages, current conversation({conversation.mode}): {conversation.id}"
        )

        return {
            "conversation_id": conversation.uuid,  # 当前会话 ID，用于区分历史消息和当前会话消息
            "conversation_mode": conversation.mode,  # 当前会话模式，用于前端展示标签
            "messages": all_messages,
        }
