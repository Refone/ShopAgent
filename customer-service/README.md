# 主业务 Service

启动命令：

```bash
cd customer-service
```

直接 uvicorn 启动

```shell
source .venv/bin/activate
uvicorn app.app:app --host 0.0.0.0 --port <port> --reload
```

main 启动

```shell
.venv/bin/python main.py
```
