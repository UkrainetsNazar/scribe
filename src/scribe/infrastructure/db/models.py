import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from scribe.infrastructure.db.base import Base


class PlanModel(Base):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)
    monthly_minutes: Mapped[int] = mapped_column(Integer)
    max_file_mb: Mapped[int] = mapped_column(Integer)


class UserModel(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    plan: Mapped[PlanModel] = relationship(lazy="joined")


class ProjectModel(Base):
    __tablename__ = "projects"
    __table_args__ = (
        Index("ix_projects_owner_id_created_at", "owner_id", "created_at"),
        Index(
            "uq_projects_owner_id_name_active",
            "owner_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    webhook_url: Mapped[str | None] = mapped_column(String(2048))
    webhook_secret: Mapped[str | None] = mapped_column(String(100))


class RecordingModel(Base):
    __tablename__ = "recordings"
    __table_args__ = (
        Index("ix_recordings_owner_id_created_at", "owner_id", "created_at"),
        Index("ix_recordings_project_id", "project_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"))
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(200))
    source_filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    duration_sec: Mapped[float | None] = mapped_column(Float)
    language: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20))
    storage_key: Mapped[str] = mapped_column(String(512))
    run: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OutboxEventModel(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        # The relay only scans rows that are still waiting to be published
        Index("ix_outbox_events_unpublished", "id", postgresql_where=text("published_at IS NULL")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True)
    aggregate_type: Mapped[str] = mapped_column(String(50))
    aggregate_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    event_type: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    headers: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InboxMessageModel(Base):
    __tablename__ = "inbox_messages"

    message_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    consumer: Mapped[str] = mapped_column(String(100), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProcessingStageModel(Base):
    __tablename__ = "processing_stages"
    __table_args__ = (UniqueConstraint("recording_id", "name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recording_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("recordings.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20))
    progress: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class UsageModel(Base):
    __tablename__ = "usage"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    period: Mapped[str] = mapped_column(String(7), primary_key=True)  # YYYY-MM
    minutes_used: Mapped[int] = mapped_column(Integer, server_default=text("0"))


class ExportModel(Base):
    __tablename__ = "exports"
    __table_args__ = (UniqueConstraint("recording_id", "format"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    recording_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("recordings.id", ondelete="CASCADE"))
    format: Mapped[str] = mapped_column(String(10))
    storage_key: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
