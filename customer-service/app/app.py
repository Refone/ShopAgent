from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import chat
from common.settings import get_settings

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.chat_router)
