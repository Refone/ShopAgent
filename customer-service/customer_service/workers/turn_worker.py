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
    """Claim conversation turns and dispatch them to the AI service."""

    HISTORY_TRACE_LIMIT: int = 20
    """Maximum number of historical messages to look back."""

    def __init__(self):
        self._worker_id = str(os.getpid())
        self._settings = get_settings()
        self._session_factory = postgresql_session_factory
        self._interval_ms = self._settings.turn_worker_pull_interval_ms

    async def start(self) -> None:
        """Start the turn worker."""

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
        """Claim and process one conversation turn.

        Returns:
            Whether a turn was claimed. The caller sleeps when no turn was claimed.
        """

        async with self._session_factory() as session:
            # Try to claim a pending conversation turn.
            turn = await self._claim_turn(session)

            if turn is None:
                return False

            LOG.info(f"Claimed turn: {turn}")

            # Build the payload required by the AI service.
            payload = await self._build_ai_request_payload(session, turn)

            # Mark the turn RUNNING to prevent duplicate claims by other workers.
            turn.status = TurnStatus.RUNNING

            # 释放 Turn 的行锁，在等待 AI Service 处理时不阻塞其他事务,
            # 如果出现插话等情况，由 _handle_ai_result 处理
            await session.commit()

            # Call the AI service and let it decide how to handle the user request.
            ai_result = await self._send_to_ai_service(payload)

            # Handle the AI response; the turn is passed in to detect interjections.
            await self._handle_ai_result(session, turn, ai_result)

            # Commit the turn status change.
            await session.commit()
            return True

    async def _claim_turn(self, session: AsyncSession) -> ConversationTurn | None:
        """Claim one turn in COLLECTING state.

        This avoids processing a turn that is already running.
        """

        # Select pending COLLECTING turns in creation order, oldest first.
        db_result = await session.execute(
            select(ConversationTurn)
            .where(ConversationTurn.status.in_([TurnStatus.COLLECTING]))
            .order_by(ConversationTurn.id)  # Process oldest first.
            .limit(1)
            .with_for_update(of=ConversationTurn, skip_locked=True)
        )

        return db_result.scalar_one_or_none()

    async def _build_ai_request_payload(
        self, session: AsyncSession, turn: ConversationTurn
    ) -> dict[str, Any]:
        """Build the AI service request payload from a turn."""

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

        # Fetch the latest N messages in reverse order, then restore chronological
        # order so the AI can understand the conversation context.
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
        """Send a claimed turn to the AI service and return its response."""
        LOG.info(f"Send AI Service request with payload: {payload}")

        # Call the AI service.
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
                        f"AI Service returned non-object JSON: {type(data).__name__}"
                    )
                return data

        except (httpx.HTTPError, TypeError, ValueError) as exc:
            LOG.exception(
                f"Conversation {payload['conversation_id']} "
                f"failed to call AI service: {exc}"
            )
            return {"status": "failed", "error": str(exc)}

    async def _handle_ai_result(
        self,
        session: AsyncSession,
        turn: ConversationTurn,
        ai_result: dict[str, Any],
    ) -> None:
        """Handle the AI service response.

        Relock the turn to check whether the result is still valid. A user
        interjection increments the revision, in which case the turn is moved
        back to COLLECTING and processed again with the new messages.
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
