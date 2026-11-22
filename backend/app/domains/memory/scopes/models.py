"""记忆控制行保留治理状态，正文由官方 Store 的不可变文档保存。"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.persistence.database import Base
from app.infrastructure.persistence.defaults import now
from app.infrastructure.persistence.defaults import uid
from app.infrastructure.persistence.memory_documents import (
    DocumentField, MemoryDocumentMixin, register_memory_document_model,
)

# PostgreSQL 和测试用 SQLite 均保留微秒，沿用当前 UTC 无时区表示。
PRECISE_TIME = DateTime()


class ScopeMemory(MemoryDocumentMixin, Base):
    __tablename__ = "scope_memories"
    __table_args__ = (UniqueConstraint("student_id", "scope_kind", "course_id", "concept_id", name="uq_scope_memory_owner"),)
    scope_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    student_id: Mapped[str] = mapped_column(String(60), index=True)
    scope_kind: Mapped[str] = mapped_column(String(16), index=True)
    course_id: Mapped[str] = mapped_column(String(60), default="", index=True)
    concept_id: Mapped[str] = mapped_column(String(96), default="", index=True)
    document_key: Mapped[str] = mapped_column(String(80), default="")
    __document_fields__ = {"content": str, "claims": list, "source_refs": list}
    content = DocumentField()
    claims = DocumentField()
    source_refs = DocumentField()
    version: Mapped[int] = mapped_column(Integer, default=0)
    source_generation: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="active")
    generation: Mapped[int] = mapped_column(Integer, default=0)
    invalidation_version: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    blocked_source_refs: Mapped[list] = mapped_column(JSON, default=list)
    blocked_claim_ids: Mapped[list] = mapped_column(JSON, default=list)
    cutoff_at: Mapped[datetime | None] = mapped_column(PRECISE_TIME, nullable=True)
    legacy_barrier: Mapped[dict] = mapped_column(JSON, default=dict)
    dependency_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[datetime] = mapped_column(PRECISE_TIME, default=now, onupdate=now)


class SessionConceptObservation(MemoryDocumentMixin, Base):
    __tablename__ = "session_concept_observations"
    __table_args__ = (UniqueConstraint("session_id", "concept_id", name="uq_session_concept_observation"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    student_id: Mapped[str] = mapped_column(String(60), index=True)
    course_id: Mapped[str] = mapped_column(String(60), index=True)
    concept_id: Mapped[str] = mapped_column(String(96), index=True)
    document_key: Mapped[str] = mapped_column(String(80), default="")
    __document_fields__ = {"content": str, "claims": list, "source_refs": list}
    content = DocumentField()
    claims = DocumentField()
    source_refs = DocumentField()
    covered_seq: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=0)
    invalidation_version: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="active")
    updated_at: Mapped[datetime] = mapped_column(PRECISE_TIME, default=now, onupdate=now)


class SessionMemoryExtraction(MemoryDocumentMixin, Base):
    """保存明确偏好及未归属事实；水位只在单会话内有效。"""

    __tablename__ = "session_memory_extractions"
    session_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    student_id: Mapped[str] = mapped_column(String(60), index=True)
    course_id: Mapped[str] = mapped_column(String(60), index=True)
    covered_seq: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=0)
    next_seq: Mapped[int] = mapped_column(Integer, default=0)
    last_enqueued_seq: Mapped[int] = mapped_column(Integer, default=0)
    closed: Mapped[bool] = mapped_column(Boolean, default=False)
    close_version: Mapped[int] = mapped_column(Integer, default=0)
    invalidation_version: Mapped[int] = mapped_column(Integer, default=0)
    document_key: Mapped[str] = mapped_column(String(80), default="")
    __document_fields__ = {"content": str, "concept_tags": list, "experience_source_refs": list,
                           "preferences": list, "source_refs": list, "unassigned": list}
    content = DocumentField()
    concept_tags = DocumentField()
    experience_source_refs = DocumentField()
    blocked_source_refs: Mapped[list] = mapped_column(JSON, default=list)
    experience_covered_seq: Mapped[int] = mapped_column(Integer, default=0)
    experience_version: Mapped[int] = mapped_column(Integer, default=0)
    experience_status: Mapped[str] = mapped_column(String(20), default="provisional")
    preferences = DocumentField()
    source_refs = DocumentField()
    unassigned = DocumentField()
    updated_at: Mapped[datetime] = mapped_column(PRECISE_TIME, default=now, onupdate=now)


for _model in (ScopeMemory, SessionConceptObservation, SessionMemoryExtraction):
    register_memory_document_model(_model)
