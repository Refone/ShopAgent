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
        Accept and process a message sent by the frontend.

        1. Get or create the conversation.
        2. Create the message entity.
        3. Route message processing by conversation mode.
        4. Commit the database session.
        """

        LOG.info(f"Accepting message from user_id={user_id} with payload={payload}")

        # Guard against duplicate submissions.
        db_result = await self._db.execute(
            select(Message).where(Message.uuid == payload.message_id)
        )
        existing_message = db_result.scalar_one_or_none()
        if existing_message:
            LOG.warning(f"Message already exists: {existing_message.uuid}")
            return payload

        # Ensure the conversation exists.
        conversation = await self._ensure_conversation(user_id)
        LOG.info(f"Ensured conversation {conversation.uuid}")

        # Create the message entity.
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

        # Route processing by conversation mode.
        match conversation.mode:
            case ConversationMode.AI:
                # AI mode.

                # Get a conversation turn:
                #   1. no active turn -> create a new turn
                #   2. a RUNNING turn exists -> the AI is replying; the user interjected
                #   3. a COLLECTING turn exists -> inside the debounce window
                turn = await self._ensure_turn(conversation)
                LOG.debug(f"Ensured turn {turn}")

                self._append_message_to_turn(turn, new_message)

            case ConversationMode.QUEUED | ConversationMode.HUMAN:
                # Human mode.
                pass
            case _:
                # Other modes.
                pass

        await self._db.commit()
        LOG.info(f"Committed message {new_message} to the database")

        return payload

    # FIXME: 感觉确认会话的逻辑应该放到前端，并且调用接口前就该搞清楚 conversation id
    async def _ensure_conversation(self, user_id: str) -> Conversation:
        """Ensure the conversation exists, creating one if needed."""

        db_result = await self._db.execute(
            select(Conversation)
            .where(
                Conversation.user_id == user_id,
                Conversation.mode.in_(
                    [
                        ConversationMode.AI,
                        ConversationMode.QUEUED,
                        ConversationMode.HUMAN,
                    ]
                ),
            )
            .with_for_update(of=Conversation)  # 锁定会话行，防止并发创建重复会话
        )

        conversation = db_result.scalar_one_or_none()

        if conversation:
            # The user has an open conversation.
            if conversation.is_expired():
                LOG.info(
                    f"Conversation[{conversation.mode}] {conversation.id} is expired, closing it"
                )
                conversation.close()
                await self._db.flush()
            else:
                # Return the active conversation (HUMAN or non-expired AI mode).
                LOG.info(
                    f"Found active conversation[{conversation.mode}] {conversation.id} for user_id={user_id}"
                )
                return conversation

        LOG.info(
            f"No active conversation found for user_id={user_id}, creating a new one"
        )
        # Create a new conversation.
        conversation = Conversation(
            user_id=user_id,
            mode=ConversationMode.AI,
        )
        self._db.add(conversation)
        await self._db.flush()  # new conversation.id required

        return conversation

    async def _ensure_turn(self, conversation: Conversation) -> ConversationTurn | None:
        """Return the active turn for the conversation, creating one if needed.

        An active turn is in COLLECTING or RUNNING state.
        """

        db_result = await self._db.execute(
            select(ConversationTurn)
            .where(
                ConversationTurn.conversation_id == conversation.id,
                ConversationTurn.status.in_(
                    [
                        TurnStatus.COLLECTING,
                        TurnStatus.RUNNING,
                    ]
                ),
            )
            .with_for_update(of=ConversationTurn)
        )

        turn = db_result.scalar_one_or_none()

        return turn or await self._create_new_turn(conversation)

    async def _create_new_turn(
        self,
        conversation: Conversation,
    ) -> ConversationTurn:
        """Create a new conversation turn for the first message."""

        turn = ConversationTurn(
            conversation_id=conversation.id,
            status=TurnStatus.COLLECTING,
            revision=0,
        )

        self._db.add(turn)
        await self._db.flush()  # 因为需要用到 turn 的 id

        return turn

    def _append_message_to_turn(
        self,
        turn: ConversationTurn,
        message: Message,
    ) -> None:
        """Append a message to the conversation turn.

        Two cases exist:
        1. COLLECTING: a new turn is open, append the message directly.
        2. RUNNING: the AI is replying and the user interjected; append the
           message to the new turn.

        This method does not branch on the case; it only bumps the revision.
        TurnWorker decides how to handle it when the AI result returns.
        """
        turn.revision += 1
        message.turn_id = turn.id

    async def get_history(self, user_id: str) -> dict[str, Any]:
        """Return the user's message history, including active and closed conversations."""

        LOG.info(f"Get History from user_id={user_id}")

        # Query all messages of the user.
        db_result = await self._db.execute(
            select(
                Message.uuid,
                Conversation.uuid.label("conversation_id"),  # Public UUID.
                Message.role,
                Message.content,
                Message.created_at,
            )
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(Conversation.user_id == user_id)
        )

        all_messages = [dict(row) for row in db_result.mappings().all()]

        conversation = await self._ensure_conversation(user_id)

        await self._db.commit()

        LOG.info(
            f"Fetched {len(all_messages)} messages, current conversation({conversation.mode}): {conversation.id}"
        )

        return {
            "conversation_id": conversation.uuid,  # Current conversation ID.
            "conversation_mode": conversation.mode,  # Mode label shown in the frontend.
            "messages": all_messages,
        }
