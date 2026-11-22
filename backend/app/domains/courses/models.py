"""课程事实与可重建检索投影的来源表。"""

from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.infrastructure.persistence.database import Base
from app.infrastructure.persistence.defaults import now


class Course(Base):
    __tablename__ = "courses"
    id: Mapped[str] = mapped_column(String(60), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    source_ref: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)


class Concept(Base):
    __tablename__ = "concepts"
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    course_id: Mapped[str] = mapped_column(String(60), index=True)
    source_id: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(240), index=True)
    topic_type: Mapped[str] = mapped_column(String(24), default="concept")
    summary: Mapped[str] = mapped_column(Text, default="")
    module: Mapped[str] = mapped_column(String(100), default="基础概念")


class Question(Base):
    __tablename__ = "questions"
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    course_id: Mapped[str] = mapped_column(String(60), index=True)
    source_id: Mapped[str] = mapped_column(String(60), index=True)
    stem: Mapped[str] = mapped_column(Text)
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    answer: Mapped[object] = mapped_column(JSON)
    question_type: Mapped[str] = mapped_column(String(24))
    cognitive_type: Mapped[str] = mapped_column(String(24), default="unknown")
    cognitive_dimension: Mapped[int | None] = mapped_column(Integer, nullable=True)
    difficulty: Mapped[str | None] = mapped_column(String(24), nullable=True)
    concept_ids: Mapped[list] = mapped_column(JSON)
    explanation: Mapped[str] = mapped_column(Text, default="")
    source_ref: Mapped[str] = mapped_column(Text)
    generated: Mapped[bool] = mapped_column(Boolean, default=False)
    validation: Mapped[dict] = mapped_column(JSON, default=dict)
    difficulty_estimate: Mapped[dict] = mapped_column(JSON, default=dict)
    teaching_reference: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)


class Material(Base):
    __tablename__ = "materials"
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    course_id: Mapped[str] = mapped_column(String(60), index=True)
    title: Mapped[str] = mapped_column(String(250))
    text: Mapped[str] = mapped_column(Text)
    concept_ids: Mapped[list] = mapped_column(JSON)
    source_ref: Mapped[str] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(String(32), default="dataset")
    visibility: Mapped[str] = mapped_column(String(24), default="public")
    question_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class ConceptRelation(Base):
    __tablename__ = "concept_relations"
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    course_id: Mapped[str] = mapped_column(String(60), index=True)
    source_id: Mapped[str] = mapped_column(String(96), index=True)
    target_id: Mapped[str] = mapped_column(String(96), index=True)
    relation_type: Mapped[str] = mapped_column(String(32))
    origin: Mapped[str] = mapped_column(String(32))
    verified: Mapped[bool] = mapped_column(Boolean, default=False)


class IndexTask(Base):
    __tablename__ = "index_tasks"
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    target: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    course_id: Mapped[str] = mapped_column(String(60), default="")
    namespace: Mapped[str] = mapped_column(String(80), default="main")
    token: Mapped[str] = mapped_column(String(64), default="")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
