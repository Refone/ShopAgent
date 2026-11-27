from enum import StrEnum


class UserRole(StrEnum):
    """
    用户角色

    Attributes:
        USER: 普通用户。
        STAFF: 客服人员。
        ADMIN: 管理员。
    """

    USER = "user"
    STAFF = "staff"
    ADMIN = "admin"


class MessageType(StrEnum):
    """
    消息类型

    Attributes:
        TEXT: 文本消息。
        OBJECT: 结构化消息，例如商品卡片、订单卡片等。
    """

    TEXT = "text"
    OBJECT = "object"


class ConversationMode(StrEnum):
    """
    会话模式

    Attributes:
        AI: AI 模式，用户与 AI 进行对话。
        QUEUED: 排队模式，用户等待人工客服接入。
        HUMAN: 人工模式，用户与人工客服进行对话。
        CLOSED: 已关闭模式，会话已结束。

        *ALIVE*: 活跃模式集合，(AI, QUEUED, HUMAN)。
    """

    AI = "AI"
    QUEUED = "QUEUED"
    HUMAN = "HUMAN"
    CLOSED = "CLOSED"


ConversationMode.ALIVE = (
    ConversationMode.AI,
    ConversationMode.QUEUED,
    ConversationMode.HUMAN,
)


class MessageRole(StrEnum):
    """
    消息角色

    Attributes:
        USER: 用户发送的消息。
        AI: AI 生成的消息。
        HUMAN: 人工客服发送的消息。
    """

    USER = "USER"
    AI = "AI"
    HUMAN = "HUMAN"


class TurnStatus(StrEnum):
    """
    轮次状态枚举

    Attributes:
        COLLECTING: 开启状态，正在收集消息。
        RUNNING: 处理中状态，正在处理消息。
        COMPLETED: 终态，处理成功。
        FAILED: 终态，处理失败。
    """

    COLLECTING = "COLLECTING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
