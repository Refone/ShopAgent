# ShopAgent

模拟用 AI Agent 赋能电商中台。让 AI 接管一部分业务流程。

## 项目结构

```text
ShopAgent
├── ai-service          # AI 相关服务
├── customer-service    # 客户相关服务
├── ecommerce-service   # 电商中台服务
└── frontend
    ├── user-frontend   # 用户前端
    └── admin-frontend  # 管理员前端
```

- User Frontend：面向普通用户，提供商城、订单和客服功能；端口：`18100`

- Admin Frontend：面向人工客服和管理员，提供人工工作台、知识管理和 AI 观测；端口：`18101`

- Customer Service：管理会话、消息、AI 调度、人工工单和实时通知；端口：`18000`

- Ecommerce Service：管理商品、订单、物流和售后业务数据；端口：`18001`

- AI Service：负责问题理解、业务查询、知识检索、回复生成和可靠性检查；端口：`18002`

## 架构总览

```mermaid
flowchart TB
    subgraph actors [系统参与者]
        Customer[普通用户]
        Agent[人工客服]
        Admin[系统管理员]
    end

    subgraph frontend [前端应用]
        UserFrontend["User Frontend :18100"]
        AdminFrontend["Admin Frontend :18101"]
    end

    subgraph backend [后端服务]
        CustomerService["Customer Service :18000"]
        EcommerceService["Ecommerce Service :18001"]
        AiService["AI Service :18002"]
    end

    subgraph infrastructure [基础设施]
        CustomerDb[(Customer PostgreSQL)]
        EcommerceDb[(Ecommerce PostgreSQL)]
        AiDb[("AI PostgreSQL + pgvector")]
        Redis[(Redis Pub/Sub)]
        LlmApi[LLM API]
        EmbeddingApi[Embedding API]
    end

    Customer --> UserFrontend
    Agent --> AdminFrontend
    Admin --> AdminFrontend

    UserFrontend -->|"REST + JWT"| CustomerService
    UserFrontend -->|"WebSocket + JWT"| CustomerService
    UserFrontend -->|"REST + JWT"| EcommerceService

    AdminFrontend -->|"REST + JWT"| CustomerService
    AdminFrontend -->|"WebSocket + JWT"| CustomerService
    AdminFrontend -->|"REST + JWT"| EcommerceService
    AdminFrontend -->|"REST + JWT"| AiService

    CustomerService -->|"NDJSON + JWT + Internal Token"| AiService
    AiService -->|"只读 REST + 用户 JWT"| EcommerceService

    CustomerService --> CustomerDb
    CustomerService --> Redis
    EcommerceService --> EcommerceDb
    AiService --> AiDb
    AiService --> LlmApi
    AiService --> EmbeddingApi
```
