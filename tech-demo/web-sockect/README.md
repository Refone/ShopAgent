# WebSocket Mini Demo

前端接受两个整数，后端计算相加结果，然后返回前端。

WebSocket 起到了 **动态通信** 的作用。

前端输入变化后可以实时发送给后端，后端计算结果后立即返回前端。

`frontend -> ws -> server -> ws -> frontend`

## 运行

```bash
cd tech-demo/web-sockect
uv run uvicorn main:app --reload
```

浏览器打开 <http://127.0.0.1:8000>。

## 核心代码

后端 `main.py`：`accept()` 建立连接，循环 `receive_json()` 读入、`send_json()` 回传和。

```python
@app.websocket("/ws")
async def websocket_sum(websocket: WebSocket):
    await websocket.accept()
    while True:
        data = await websocket.receive_json()
        await websocket.send_json({"sum": int(data["a"]) + int(data["b"])})
```

前端 `demo.html`：`new WebSocket()` 连到 `/ws`，`ws.send()` 发送输入，`onmessage` 收到结果写回框3。

```js
const ws = new WebSocket(`ws://${location.host}/ws`);
ws.onopen = () => send();
ws.onmessage = (e) => (result.textContent = JSON.parse(e.data).sum);
function send() {
  ws.send(JSON.stringify({ a: num1.valueAsNumber, b: num2.valueAsNumber }));
}
```
