import asyncio

from customer_service.common.settings import get_settings
from customer_service.infra.pgsql import get_db_session


class TurnWorker:
    """
    轮询 ConversationTurn 表, 领取会话轮次, 交给 AI Service
    """

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

            self._send_to_ai_service(turn)

    async def _claim_turn(self):
        """
        领取一个会话轮次
        """

    def _send_to_ai_service(self, turn):
        """
        将领取到的 turn 交给 AI Service 处理
        """
