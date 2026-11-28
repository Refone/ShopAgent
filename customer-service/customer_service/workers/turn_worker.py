import asyncio
import os
from typing import Any

import httpx
from sqlalchemy import select

from customer_service.common.settings import get_settings
from customer_service.infra.logger import LOG
from customer_service.infra.pgsql import AsyncSession, postgresql_session_factory
from customer_service.schemas.entities import ConversationTurn, Message
from customer_service.schemas.enums import TurnStatus


class TurnWorker:
    """
    轮询 ConversationTurn 表, 领取会话轮次, 交给 AI Service
    """

    HISTORY_TRACE_LIMIT: int = 20
    """历史消息往前回溯的最大条数"""

    def __init__(self):
        self._worker_id = str(os.getpid())
        self._settings = get_settings()
        self._session_factory = postgresql_session_factory
        self._interval_ms = self._settings.turn_worker_pull_interval_ms

    async def start(self) -> None:
        """
        启动 turn-worker
        """

        while True:
            try:
                processed = await self._claim_turn_and_process()
            except asyncio.CancelledError:
                raise
            # 兜底所有异常，保证轮询任务不会因单个轮次失败而退出
            except Exception as exc:  # noqa: BLE001
                LOG.exception(f"TurnWorker on exception: {exc}")
                processed = False

            if not processed:
                await asyncio.sleep(self._interval_ms / 1000)

    async def _claim_turn_and_process(self) -> bool:
        """
        领取并处理一个会话轮次

        Returns:
            本轮是否领取到轮次；未领取到时由调用方按轮询间隔休眠。
        """

        async with self._session_factory() as session:
            # 尝试领取一个待处理的会话轮次
            turn = await self._claim_turn(session)

            if turn is None:
                return False

            LOG.info(f"Claimed turn: {turn}")

            # 构建 AI Service 需要的 payload
            payload = await self._build_ai_request_payload(session, turn)

            # 更新 turn 的状态 COLLECTING -> RUNNING, 避免被其他 worker 重复领取
            turn.status = TurnStatus.RUNNING

            # 释放 Turn 的行锁，在等待 AI Service 处理时不阻塞其他事务,
            # 如果出现插话等情况，由 _handle_ai_result 处理
            await session.commit()

            # 由 worker 去请求 AI service，通过 ai 判断如何处理用户询问
            ai_result = await self._send_to_ai_service(payload)

            # 处理 AI Service 的响应结果, 传入 turn 是需要判断有无插话
            await self._handle_ai_result(session, turn, ai_result)

            # 提交 Turn 的状态变化
            await session.commit()
            return True

    async def _claim_turn(self, session: AsyncSession) -> ConversationTurn | None:
        """
        领取一个会话轮次，只领取 COLLECTING 状态，避免重复处理进行中的轮次
        """

        # 查询数据库中处于运行状态的会话轮次，按创建顺序先到先处理
        db_result = await session.execute(
            select(ConversationTurn)
            .where(ConversationTurn.status.in_([TurnStatus.COLLECTING]))
            .order_by(ConversationTurn.id)  # 先到先处理
            .limit(1)
            .with_for_update(of=ConversationTurn, skip_locked=True)
        )

        return db_result.scalar_one_or_none()

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
                Message.turn_id == turn.id,
            )
            .order_by(Message.id)
        )
        current_turn_messages = result.scalars().all()

        # 理论不该有这种情况，轮次创建流程中，Message.turn_id 被正确设置后才会入库
        if not current_turn_messages or len(current_turn_messages) == 0:
            raise ValueError(f"Turn {turn.id} has no messages")

        first_message_id = current_turn_messages[0].id

        # 先按倒序取最近 N 条，再翻转回时间正序，供 AI 理解上下文
        result = await session.execute(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.id < first_message_id,
            )
            .order_by(Message.id.desc())
            .limit(self.HISTORY_TRACE_LIMIT)
        )
        history_messages = list(reversed(result.scalars().all()))

        return {
            "conversation_id": conversation_id,
            "turn_id": turn.id,
            "turn_revision": turn.revision,
            "messages": [
                {
                    "role": message.role,
                    "type": message.message_type,
                    "content": message.content,
                }
                for message in current_turn_messages
            ],
            "history": [
                {
                    "role": history.role,
                    "type": history.message_type,
                    "content": history.content,
                }
                for history in history_messages
            ],
        }

    async def _send_to_ai_service(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        将领取到的 turn 交给 AI Service 处理，并返回 AI Service 的响应结果
        """
        LOG.info(f"Send AI Service request with payload: {payload}")

        # 调用 AI Service 处理请求
        try:
            timeout = httpx.Timeout(self._settings.ai_timeout_seconds, connect=5)
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                response = await client.post(
                    f"{self._settings.ai_service_url}/internal/v1/agent/runs",
                    # headers=headers,
                    json=payload,
                )

                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise TypeError(
                        f"AI Service 返回了非对象 JSON: {type(data).__name__}"
                    )
                return data

        except (httpx.HTTPError, TypeError, ValueError) as exc:
            LOG.exception(
                f"会话 {payload['conversation_id']} 调用 AI Service 失败: {exc}"
            )
            return {"status": "failed", "error": str(exc)}

    async def _handle_ai_result(
        self,
        session: AsyncSession,
        turn: ConversationTurn,
        ai_result: dict[str, Any],
    ) -> None:
        """
        处理 AI Service 的响应结果

        重新锁定轮次，判断结果是否仍然有效：用户插话会递增 revision，
        此时把轮次退回 COLLECTING，让下一次请求携带新消息重新处理。
        """
        LOG.info(f"AI service returned result: {ai_result}")

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

        if now_turn.status != TurnStatus.RUNNING:
            LOG.warning(
                f"Turn {turn.id} status changed to {now_turn.status}, discard AI result"
            )
            return

        # AI Service 调用失败时标记 FAILED，避免轮次被反复领取
        if ai_result.get("status") != "success":
            now_turn.status = TurnStatus.FAILED
            LOG.warning(f"Turn {turn.id} AI service failed: {ai_result}")
            return

        # 用户插话产生了新的 revision，当前结果已过期，退回 COLLECTING 重新处理
        if ai_result.get("revision") != now_turn.revision:
            LOG.info(
                f"Turn {turn.id} revision changed "
                f"{ai_result.get('revision')} -> {now_turn.revision}, requeue"
            )
            now_turn.status = TurnStatus.COLLECTING
            return

        # FIXME: ai-service 尚未定义落库契约，AI 回答暂不写回消息表。
        now_turn.status = TurnStatus.COMPLETED
