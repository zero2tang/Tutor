"""该业务功能拥有的持久化记录；表名、字段与约束保持原有契约。"""

from app.infrastructure.persistence.database import Base
from sqlalchemy import DateTime, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime
from app.infrastructure.persistence.defaults import now, uid


class LearningSession(Base):
    __tablename__ = "learning_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    course_id: Mapped[str] = mapped_column(String(60), index=True)
    student_id: Mapped[str] = mapped_column(String(60), default="local")
    title: Mapped[str] = mapped_column(String(200))
    goal: Mapped[str] = mapped_column(Text)
    agenda: Mapped[list] = mapped_column(JSON, default=list)
    concept_ids: Mapped[list] = mapped_column(JSON, default=list)
    phase: Mapped[str] = mapped_column(String(24), default="learning")
    pending_question_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    pending_context: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=0)
    active_turn_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    role: Mapped[str] = mapped_column(String(24))
    agent: Mapped[str] = mapped_column(String(24), default="")
    content: Mapped[str] = mapped_column(Text)
    citations: Mapped[list] = mapped_column(JSON, default=list)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class SessionGoalState(Base):
    __tablename__ = "session_goal_states"

    session_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    root_goal_id: Mapped[str] = mapped_column(String(80))
    active_goal_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    nodes: Mapped[list] = mapped_column(JSON, default=list)
    return_stack: Mapped[list] = mapped_column(JSON, default=list)
    next_action: Mapped[str] = mapped_column(String(1000), default="")
    version: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
