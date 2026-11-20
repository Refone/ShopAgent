from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from customer_service.app.routers import chat
from customer_service.common.settings import get_settings

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.chat_router)
