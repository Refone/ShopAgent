from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from customer_service.common.settings import get_settings
from customer_service.common.utils import get_uid, get_utcnow
from customer_service.schemas.enums import (
    ConversationMode,
    MessageType,
    TurnStatus,
)


class Base(DeclarativeBase):
    """
    数据库实体的 SQLAlchemy 声明式基类，共享表元数据，本身不映射数据库表。
    """


class Conversation(Base):
    """
    用户的客服会话，记录服务模式和活跃时间，并关联消息与处理轮次。

    同一用户最多只能有一个 AI、QUEUED 或 HUMAN 模式的会话，
    已关闭的历史会话不受此唯一性约束限制。

    Attributes:
        id: 系统内部使用的 64 位自增主键。
        uuid: 会话唯一标识，由服务端生成，供外部系统使用。
        user_id: 会话所属用户的标识。
        mode: 会话模式，支持 AI、QUEUED、HUMAN 和 CLOSED，默认为 AI。
        started_at: 会话创建时间，使用带时区的 UTC 时间。
        last_active_at: 最近活跃时间，用于判断会话是否因空闲而过期。
    """

    __tablename__ = "conversations"
    __table_args__ = (
        # 同一个 user_id 只能有一个 AI ｜ QUEUED ｜ HUMAN 模式的会话，CLOSED 的会话不受此约束
        Index(
            "uq_open_conversation_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("mode IN ('AI', 'QUEUED', 'HUMAN')"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    uuid: Mapped[str] = mapped_column(
        String(80), unique=True, default=lambda: get_uid("conv")
    )
    user_id: Mapped[str] = mapped_column(String(40), index=True)
    mode: Mapped[str] = mapped_column(
        String(20), default=ConversationMode.AI, index=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=get_utcnow
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=get_utcnow, onupdate=get_utcnow
    )

    def update_last_active(self) -> None:
        """
        更新会话的最近活跃时间为当前 UTC 时间，用于判断会话是否过期。
        """

        self.last_active_at = get_utcnow()

    def is_expired(
        self,
        expiration_seconds: int = get_settings().conversation_idle_timeout_seconds,
    ) -> bool:
        """
        会话是否超时过期，
        AI 模式会话超时自动关闭，并创建新的 AI 模式会话
        Human | QUEUE 模式会话不会自动关闭，直到人工客服手动关闭
        """
        return (
            self.mode == ConversationMode.AI
            and get_utcnow()
            > self.last_active_at + timedelta(seconds=expiration_seconds)
        )

    def close(self) -> None:
        """
        关闭会话，将会话模式设置为 CLOSED，表示会话已结束。
        """
        self.mode = ConversationMode.CLOSED


class Message(Base):
    """
    会话中的一条消息，用于保存用户、AI 或人工客服的聊天记录。

    数据库自增主键与业务消息标识分离，业务消息标识的唯一约束用于防止重复入库。

    Attributes:
        id: 系统内部使用的 64 位自增主键。
        uuid: 业务消息唯一标识，用户消息由前端生成，用于重复提交检查。
        conversation_id: 所属会话的外键，关联 conversations.id。
        role: 消息发送方角色，使用 USER、AI 或 HUMAN。
        message_type: 消息类型，支持 text 和 object，默认为 text。
        content: 消息内容，以 JSONB 保存文本或消息卡片等结构化数据。
        created_at: 消息创建时间，使用带时区的 UTC 时间。
    """

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    uuid: Mapped[str] = mapped_column(String(80), unique=True)
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("conversations.id"), index=True
    )
    role: Mapped[str] = mapped_column(String(20))
    message_type: Mapped[str] = mapped_column(String(20), default=MessageType.TEXT)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=get_utcnow
    )


class ConversationTurn(Base):
    """
    AI 模式会话中的一个会话轮次, 一问一答、多问一答(插话追问)、一问多答。

    同一会话最多只能有一个 OPEN 状态的轮次，

    Attributes:
        id: 系统内部使用的 64 位自增主键。
        uuid: 轮次唯一标识，由服务端生成，供外部系统使用。
        conversation_id: 所属会话的外键，关联 conversations.id。
        status: 轮次状态，取值见 TurnStatus, 初始为 OPEN。
        start_message_id: 轮次第一条消息 ID, 关联 messages.id。
        end_message_id: 轮次最后一条消息 ID, 关联 messages.id。
    """

    __tablename__ = "conversation_turns"
    __table_args__ = (
        # 同一个 conversation_id 只能有一个 OPEN 状态的轮次，其他状态不受此约束
        Index(
            "uq_alive_turn_per_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=text("status = 'OPEN'"),
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    uuid: Mapped[str] = mapped_column(
        String(80), unique=True, default=lambda: get_uid("turn")
    )
    conversation_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("conversations.id"),
        index=True,
    )
    status: Mapped[str] = mapped_column(String(30), default=TurnStatus.OPEN, index=True)
    start_message_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("messages.id"),
    )
    end_message_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("messages.id"),
    )


class RealtimeEvent(Base):
    """
    持久化的实时事件，供实时推送 Worker 异步发布到目标通道。

    事件可与业务数据在同一事务内保存，发布进度通过 published_at 记录。

    Attributes:
        id: 事件的 64 位自增主键。
        target: 目标通道，当前使用用户 ID 或人工客服公共通道 staff。
        data: 待推送的事件载荷，以 JSONB 保存。
        created_at: 事件创建时间，使用带时区的 UTC 时间。
        published_at: 事件发布时间，为空表示尚未标记为已发布。
    """

    __tablename__ = "realtime_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    target: Mapped[str] = mapped_column(String(80), index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=get_utcnow
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
