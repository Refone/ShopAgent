from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from customer_service.models.conversation import ConversationTurn


class ConversationTurnRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def has_active_turn(self, conversation_id: str) -> bool:
        return await self._session.scalar(
            select(
                exists().where(
                    ConversationTurn.conversation_id == conversation_id,
                    ConversationTurn.status.in_(["RUNNING", "COLLECTING"]),
                )
            )
        )

    async def find_collecting_turn(
        self,
        conversation_id: str,
    ) -> ConversationTurn | None:
        return await self._session.scalar(
            select(ConversationTurn)
            .where(
                ConversationTurn.conversation_id == conversation_id,
                ConversationTurn.status.in_(["COLLECTING"]),
            )
            .with_for_update()
        )

    def add_turn(self, turn: ConversationTurn):
        self._session.add(turn)
