# 模拟电商中台

以封装 docker，启动方式：

```shell
cd ecommerce-service

docker compose up -d --build      # 构建镜像 + 后台拉起 postgres/redis/app
docker compose ps                 # 三个容器都应为 healthy
docker compose logs -f app        # 跟随应用日志
curl http://127.0.0.1:18001/health
```

停止与清理：

```shell
docker compose down      # 停止容器，保留数据卷
docker compose down -v   # 连数据卷一起删，下次启动重新初始化数据库
```

各种端口绑定见 `.env`

docker 启动成功标志：

- `docker compose ps` 中三个容器都为 `healthy`
- 访问 `http://127.0.0.1:18001/docs` 能进入 Swagger 页面
