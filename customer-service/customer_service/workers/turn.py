import asyncio
from typing import Any

from sqlalchemy import select

from customer_service.common.settings import get_settings
from customer_service.infra.logger import LOG
from customer_service.infra.pgsql import get_db_session
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
        self._db_session = get_db_session()
        self._interval_ms = self._settings.turn_worker_pull_interval_ms

    async def start(self):
        """
        启动 turn-worker
        """

        while True:
            turn = await self._claim_turn()
            if not turn:
                await asyncio.sleep(self._interval_ms / 1000)
                continue
            LOG.info(f"Claimed turn: {turn}")

            payload = await self._build_ai_request_payload(turn)
            LOG.info(f"Sending turn to AI service: {payload}")

            # 释放 Turn 的行锁，在等待 AI Service 处理时不阻塞其他事务
            await self._db_session.commit()

            # 由 worker 去请求 AI service，通过 ai 判断如何处理用户询问
            ai_result = await self._send_to_ai_service(payload)
            LOG.info(f"AI service returned result: {ai_result}")

            # 处理 AI Service 的响应结果, 传入 turn 是需要判断有无插话
            await self._handle_ai_result(turn, ai_result)

    async def _claim_turn(self) -> ConversationTurn | None:
        """
        领取一个会话轮次
        """

        # 查询数据库中处于 OPEN 状态的会话轮次
        turn = await self._db_session.execute(
            select(ConversationTurn)
            .where(ConversationTurn.status == TurnStatus.OPEN)
            .with_for_update(of=ConversationTurn)
        )
        turn = turn.scalar_one_or_none()
        return turn

    async def _build_ai_request_payload(self, turn: ConversationTurn) -> dict[str, Any]:
        """
        根据 turn 构建发送给 AI Service 的请求 payload
        """

        conversation_id = turn.conversation_id

        result = await self._db_session.execute(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.id >= turn.start_message_id,
                Message.id <= turn.end_message_id,
            )
            .order_by(Message.id)
        )
        current_turn_messages = result.scalars().all()

        result = await self._db_session.execute(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.id < turn.start_message_id,
            )
            .order_by(Message.id)
            .limit(self.HISTORY_TRACE_LIMIT)
        )
        history_messages = result.scalars().all()

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

    async def _send_to_ai_service(self, payload) -> dict[str, Any]:
        """
        将领取到的 turn 交给 AI Service 处理，并返回 AI Service 的响应结果
        """
        return {}  # 这里应返回 AI Service 的响应结果，暂时返回空字典作为占位

    async def _handle_ai_result(
        self, turn: ConversationTurn, ai_result: dict[str, Any]
    ):
        """
        处理 AI Service 的响应结果
        """

        # 查询 ai 刚刚处理完的轮次，看一看用户有无补充新消息
        now_turn = await self._db_session.get(
            ConversationTurn, turn.id
        ).with_for_update()

        # 首先判断 AI Service 的响应结果是否过期
        if now_turn.end_message_id < turn.end_message_id:
            # 用户有补充新消息，AI 处理结果过期，丢弃 AI 处理结果，do nothing
            LOG.info(
                f"Turn {turn.id} is expired, newest message {turn.end_message_id} -> {now_turn.end_message_id}"
            )
            await self._db_session.commit()
            return

        # 判断 AI Service 的响应结果是否有效

        # 如果有效，将 AI 回答添加到当前轮次，并标记轮次状态为 COMPLETED
        # 如果无效，标记轮次状态为 FAILED，提示前端可重试。

        await self._db_session.commit()
