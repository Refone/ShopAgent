from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from customer_service.models.conversation import Conversation


class ConversationRepository:
    # repo 层其他地方仅 flush
    # commit 交给 service 层来做

    def __init__(self, session: AsyncSession):
        self._session = session

    async def latest_active_conversation(
        self,
        user_id: str,
        mode: tuple[str, ...],
    ) -> Conversation | None:
        """
        查询指定用户的最新活跃会话。

        :param user_id: 用户 ID
        :param mode: 会话模式列表
        :return: 活跃会话，如果没有则返回 None
        """

        return await self._session.scalar(
            select(Conversation)
            .where(
                Conversation.user_id == user_id,
                Conversation.mode.in_(mode),
            )
            .order_by(Conversation.last_active_at.desc())
            .limit(1)
        )

    async def add_conversation(
        self,
        user_id: str,
        mode: str = "AI",
    ) -> Conversation:
        """
        创建一个新的会话。

        :param user_id: 用户 ID
        :param mode: 会话模式，默认为 "AI"
        :return: 创建的会话实例
        """

        conversation = Conversation(
            user_id=user_id,
            mode=mode,
        )

        self._session.add(conversation)

        await self._session.flush()

        return conversation

    async def find_conversations(
        self,
        user_id: str,
        mode: str,
    ) -> list[Conversation]:
        """
        查询指定用户的所有指定模式的会话。

        :param user_id: 用户 ID
        :param mode: 会话模式
        :return: 指定模式的会话列表
        """

        result = await self._session.scalars(
            select(Conversation).where(
                Conversation.user_id == user_id,
                Conversation.mode == mode,
            )
        )

        return list(result.all())

    async def close_conversation(self, conversation: Conversation) -> None:
        """
        关闭指定的会话，将其状态改为 "CLOSED" 并设置结束时间。

        :param conversation: 要关闭的会话实例
        :return: None
        """

        # SQLAlchemy ORM 有一个机制叫 Dirty Tracking（脏数据跟踪）
        # 只要 conversation 是当前 SQLAlchemy Session 所管理的 ORM 实例，
        # 直接修改属性后，调用 await self.session.flush() 就能将修改同步到数据库。
        conversation.ended_at = conversation.last_active_at
        conversation.mode = "CLOSED"

        await self._session.flush()

    async def get_conversation_by_id(self, conversation_id: str) -> Conversation | None:
        """
        根据会话 ID 获取会话实例。

        :param conversation_id: 会话 ID
        :return: 会话实例，如果不存在则返回 None
        """

        return await self._session.get(Conversation, conversation_id)
