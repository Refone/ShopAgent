from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from customer_service.app.routers.conversation import chat_router
from customer_service.app.routers.message import message_router
from customer_service.common.config import get_settings

app = FastAPI(description="FastAPI集成的客服服务")

app.include_router(chat_router)
app.include_router(message_router)


app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
