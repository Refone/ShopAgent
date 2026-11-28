"""WebSocket 最小实践：前端发送两个整数，后端求和后回传。"""

from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

# 页面与后端放在同一目录，由后端直接托管
BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="WebSocket Sum Demo")


@app.get("/")
async def index() -> FileResponse:
    """Serve the demo page."""
    return FileResponse(BASE_DIR / "demo.html")


@app.websocket("/ws")
async def websocket_sum(websocket: WebSocket) -> None:
    """Receive two integers over WebSocket and reply with their sum."""
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_json()
            # 输入不是两个整数时回传错误提示，而不是断开连接
            try:
                a = int(data["a"])
                b = int(data["b"])
            except (KeyError, TypeError, ValueError):
                await websocket.send_json({"error": "请输入两个整数"})
                continue
            await websocket.send_json({"sum": a + b})
    except WebSocketDisconnect:
        # 客户端断开后正常退出
        pass
