from fastapi import APIRouter

chat_router = APIRouter(
    prefix="/api/v1/chat",
    tags=["会话管理路由: 对话、加载历史消息会话"],
)


@chat_router.post("/messages")
async def post_messages():
    return {"kick_off": "ok"}
