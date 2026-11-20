from sqlalchemy.ext.asyncio import AsyncSession


class ChatService:
    def __init__(self, db_session: AsyncSession):
        self._db = db_session

    async def accept_message(self, payload):
        return payload
