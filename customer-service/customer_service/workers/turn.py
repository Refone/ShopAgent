import asyncio
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from customer_service.common.settings import get_settings
from customer_service.infra.logger import LOG
from customer_service.infra.pgsql import postgresql_session_factory
from customer_service.schemas.entities import ConversationTurn, Message
from customer_service.schemas.enums import TurnStatus


class TurnWorker:
    """
    轮询 ConversationTurn 表, 领取会话轮次, 交给 AI Service
    """

    HISTORY_TRACE_LIMIT: int = 20
    """历史消息往前回溯的最大条数"""

    def __init__(self):
        self._settings = get_settings()
        self._session_factory = postgresql_session_factory
        self._interval_ms = self._settings.turn_worker_pull_interval_ms

    async def start(self) -> None:
        """
        启动 turn-worker
        """

        while True:
            turn: ConversationTurn | None = None
            payload: dict[str, Any] | None = None

            async with self._session_factory() as session:
                turn = await self._claim_turn(session)
                if turn is not None:
                    LOG.info(f"Claimed turn: {turn.id}")

                    payload = await self._build_ai_request_payload(session, turn)
                    LOG.info(f"Sending turn to AI service: {payload}")

                    # 释放 Turn 的行锁，在等待 AI Service 处理时不阻塞其他事务
                    await session.commit()

            if turn is None:
                await asyncio.sleep(self._interval_ms / 1000)
                continue

            assert payload is not None
            # 由 worker 去请求 AI service，通过 ai 判断如何处理用户询问
            ai_result = await self._send_to_ai_service(payload)
            LOG.info(f"AI service returned result: {ai_result}")

            # 处理 AI Service 的响应结果, 传入 turn 是需要判断有无插话
            async with self._session_factory() as session:
                await self._handle_ai_result(session, turn, ai_result)

    async def _claim_turn(self, session: AsyncSession) -> ConversationTurn | None:
        """
        领取一个会话轮次
        """

        # 查询数据库中处于运行状态的会话轮次，按创建顺序先到先处理
        result = await session.execute(
            select(ConversationTurn)
            .where(
                ConversationTurn.status.in_(
                    [
                        TurnStatus.COLLECTING,
                        TurnStatus.RUNNING,
                    ]
                )
            )
            .order_by(ConversationTurn.id)
            .limit(1)
            .with_for_update(of=ConversationTurn)
        )

        return result.scalar_one_or_none()

    async def _build_ai_request_payload(
        self, session: AsyncSession, turn: ConversationTurn
    ) -> dict[str, Any]:
        """
        根据 turn 构建发送给 AI Service 的请求 payload
        """

        conversation_id = turn.conversation_id

        result = await session.execute(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.id >= turn.start_message_id,
                Message.id <= turn.end_message_id,
            )
            .order_by(Message.id)
        )
        current_turn_messages = result.scalars().all()

        # 先按倒序取最近 N 条，再翻转回时间正序，供 AI 理解上下文
        result = await session.execute(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.id < turn.start_message_id,
            )
            .order_by(Message.id.desc())
            .limit(self.HISTORY_TRACE_LIMIT)
        )
        history_messages = list(reversed(result.scalars().all()))

        return {
            "conversation_id": conversation_id,
            "messages": [
                {
                    "role": message.role,
                    "content": message.content,
                    "type": message.message_type,
                }
                for message in current_turn_messages
            ],
            "history": [
                {
                    "role": history.role,
                    "content": history.content,
                    "type": history.message_type,
                }
                for history in history_messages
            ],
        }

    async def _send_to_ai_service(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        将领取到的 turn 交给 AI Service 处理，并返回 AI Service 的响应结果
        """
        return {}  # 这里应返回 AI Service 的响应结果，暂时返回空字典作为占位

    async def _handle_ai_result(
        self,
        session: AsyncSession,
        turn: ConversationTurn,
        ai_result: dict[str, Any],
    ) -> None:
        """
        处理 AI Service 的响应结果
        """

        # 锁定轮次行，避免处理结果期间用户继续追加消息
        result = await session.execute(
            select(ConversationTurn)
            .where(ConversationTurn.id == turn.id)
            .with_for_update(of=ConversationTurn)
        )
        now_turn = result.scalar_one_or_none()

        if now_turn is None:
            LOG.warning(f"Turn {turn.id} no longer exists, discard AI result")
            await session.rollback()
            return

        # 首先判断 AI Service 的响应结果是否过期
        if now_turn.end_message_id > turn.end_message_id:
            # 用户有补充新消息，AI 处理结果过期，丢弃 AI 处理结果，保留 OPEN 等待下一轮
            LOG.info(
                f"Turn {turn.id} is expired, captured end message "
                f"{turn.end_message_id}, newest message {now_turn.end_message_id}"
            )
            await session.commit()
            return

        if now_turn.status != TurnStatus.COLLECTING:
            LOG.warning(
                f"Turn {turn.id} status changed to {now_turn.status}, discard AI result"
            )
            await session.commit()
            return

        # 空结果视为无效，标记 FAILED 供前端重试，避免轮次被反复领取
        if not ai_result:
            now_turn.status = TurnStatus.FAILED
            LOG.warning(f"Turn {turn.id} got empty AI result, mark as FAILED")
            await session.commit()
            return

        # 如果有效，将 AI 回答添加到当前轮次，并标记轮次状态为 COMPLETED
        # FIXME: ai-service 尚未实现，接口契约未定义，落库逻辑待契约确定后补充。

        await session.commit()
