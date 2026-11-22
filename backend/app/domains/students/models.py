"""该业务功能拥有的持久化记录；表名、字段与约束保持原有契约。"""

from app.infrastructure.persistence.database import Base
from sqlalchemy import DateTime, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime
from app.infrastructure.persistence.defaults import now, uid


class StudentState(Base):
    __tablename__ = "student_states"
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    student_id: Mapped[str] = mapped_column(String(60), default="local")
    concept_id: Mapped[str] = mapped_column(String(96), index=True)
    mastery: Mapped[dict] = mapped_column(
        JSON, default=lambda: {"beginner": None, "advanced": None, "proficient": None}
    )
    evidence_ids: Mapped[list] = mapped_column(JSON, default=list)
    evidence_questions: Mapped[list] = mapped_column(JSON, default=list)
    misconceptions: Mapped[list] = mapped_column(JSON, default=list)
    version: Mapped[int] = mapped_column(Integer, default=0)
    next_review_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class MemoryItem(Base):
    __tablename__ = "memory_items"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    student_id: Mapped[str] = mapped_column(String(60), default="local")
    course_id: Mapped[str] = mapped_column(String(60), index=True)
    concept_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    kind: Mapped[str] = mapped_column(String(24))
    text: Mapped[str] = mapped_column(Text)
    evidence_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(24), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
