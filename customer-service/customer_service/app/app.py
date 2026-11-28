import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from customer_service.app.routers import chat
from customer_service.common.settings import get_settings
from customer_service.infra.logger import LOG
from customer_service.workers.turn_worker import TurnWorker


@asynccontextmanager
async def lifespan(_: FastAPI):
    worker = asyncio.create_task(TurnWorker().start())
    try:
        yield
    finally:
        worker.cancel()
        results = await asyncio.gather(worker, return_exceptions=True)
        exc = results[0]
        if isinstance(exc, Exception) and not isinstance(exc, asyncio.CancelledError):
            LOG.exception("TurnWorker 意外退出", exc_info=exc)


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.chat_router)
