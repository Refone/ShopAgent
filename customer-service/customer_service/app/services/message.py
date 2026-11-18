from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from customer_service.app.repositories.message import MessageRepository
from customer_service.app.schemas.event import STAFF_CHANNEL, RealTimeOutBoxType
from customer_service.app.schemas.message import ChatMessageRequest
from customer_service.app.services.conversation import ConversationService
from customer_service.app.services.realtime import RealTimeOutBoxService
from customer_service.app.services.turn import TurnService
from customer_service.common.utils import get_uid, get_utcnow
from customer_service.models.conversation import Conversation, Message


def parse_message_event_data(message: Message) -> dict[str, Any]:
    return {
        "message": {
            "message_id": message.id,
            "role": message.role,
            "content": message.content,
        }
    }


class MessageService:
    def __init__(
        self,
        session: AsyncSession,
        conversation_service: ConversationService,
        outbox_service: RealTimeOutBoxService,
        turn_service: TurnService,
    ):
        self._session = session
        self._message_repo = MessageRepository(self._session)
        self._conversation_service = conversation_service
        self._turn_service = turn_service
        self._outbox_service = outbox_service

    async def accept_user_message(
        self,
        chat_message: ChatMessageRequest,  # 更标准应该是 domain 数据模型
        user_id: str,
    ) -> dict[str, Any]:
        """
        保存用户消息
        """
        # 根据消息 ID 以及消息所在的会话，查询是否存在重复的消息
        already_in_conversation: (
            Conversation | None
        ) = await self._message_repo.find_conversation_by_message_id(
            chat_message.message_id
        )

        if already_in_conversation:
            return {
                "conversation_id": already_in_conversation.id,
                "mode": already_in_conversation.mode,
            }

        # 当前用户消息的会话
        conversation: Conversation = (
            await self._conversation_service.ensure_locked_active_conversation(user_id)
        )

        # 如果不存在重复消息，则保存消息
        message = self.save_message(
            conversation=conversation,
            message_content=chat_message.content,
            message_id=chat_message.message_id,
            message_role="user",
            message_type=chat_message.type,
        )

        # 判断会话的模式
        if conversation.mode == "AI":
            # 管理轮次
            await self._turn_service.add_message_to_turn(message, conversation)

        elif conversation.mode in ("HUMAN", "QUEUED"):
            # 推送管理端、MESSAGE_CREATE、当前消息
            self._outbox_service.add_realtime_outbox(
                event_channel=STAFF_CHANNEL,
                event_type=RealTimeOutBoxType.MESSAGE_CREATE,
                event_data=parse_message_event_data(message),
                conversation_id=conversation.id,
                message_id=message.id,
            )
        else:
            raise ValueError("未知的会话模式")

        # 提交事务
        await self._session.commit()

        # 返回接口需要的数据
        return {
            "conversation_id": conversation.id,
            "mode": conversation.mode,
        }

    def save_message(
        self,
        conversation: Conversation,
        message_content: dict[str, Any],
        message_id: str | None,
        *,
        message_role: str,
        message_type: str = "text",
    ) -> Message:
        # 实例化消息对象
        message = Message(
            conversation_id=conversation.id,
            role=message_role,
            message_id=message_id or get_uid(conversation.id),
            message_type=message_type,
            content=message_content or {},
        )

        # 更新会话激活时间
        conversation.last_active_at = get_utcnow()

        # 入库
        self._message_repo.add_message(message)

        return message
