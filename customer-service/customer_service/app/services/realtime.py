from sqlalchemy.ext.asyncio import AsyncSession

from customer_service.schemas.entities import Conversation, Message, RealtimeEvent


class RealtimeService:
    def __init__(self, db_session: AsyncSession):
        self.db_session = db_session

    def add_event(
        self,
        message: Message,
        conversation: Conversation,
        *,
        notify_user: bool = False,
        notify_staff: bool = False,
    ):
        """Add realtime events for the given message and conversation."""

        # 1. Determine target channels.
        target_channels = []
        if notify_user:
            target_channels.append(conversation.user_id)
        if notify_staff:
            target_channels.append("staff")

        # 2. Build the event payload.
        event_data = {
            "role": message.role,  # Rendering hint for the frontend: AI or human.
            "content": message.content,  # Message content shown in the frontend.
            # Badge label in the frontend; keeps the badge updated in realtime.
            "conversation_mode": conversation.mode,
        }

        # 3. Iterate over target channels.
        for target_channel in target_channels:
            self.db_session.add(
                RealtimeEvent(target=target_channel, data=event_data)
            )  # 不会利用event_data，客服端只是作为一次信号，调用刷新工单列表接口
