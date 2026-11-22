"""两类模型记忆及其可恢复的事实水位和后台任务。"""

from datetime import datetime
from sqlalchemy import Boolean, DateTime, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.infrastructure.persistence.database import Base
from app.infrastructure.persistence.defaults import now, uid


class MemoryFact(Base):
    __tablename__ = "memory_facts"
    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="uq_memory_fact_seq"),
        UniqueConstraint("session_id", "source_ref", name="uq_memory_fact_source"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    source_ref: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(24))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class MemoryJob(Base):
    __tablename__ = "memory_jobs"
    __table_args__ = (
        UniqueConstraint(
            "job_kind",
            "session_id",
            "target_seq",
            "extractor_version",
            "close_version",
            name="uq_memory_job_watermark",
        ),
        UniqueConstraint(
            "job_kind",
            "scope_key",
            "source_generation",
            "rule_version",
            name="uq_memory_job_scope",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    session_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    target_seq: Mapped[int] = mapped_column(Integer, default=0)
    extractor_version: Mapped[str] = mapped_column(String(24), default="model-memory-v1")
    close_version: Mapped[int] = mapped_column(Integer, default=0)
    final: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(String(40), default="scope")
    job_kind: Mapped[str] = mapped_column(
        String(24), default="session_extract", index=True
    )
    scope_key: Mapped[str | None] = mapped_column(
        String(160), nullable=True, index=True
    )
    source_generation: Mapped[int] = mapped_column(Integer, default=0)
    rule_version: Mapped[str] = mapped_column(String(32), default="")
    result_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    input_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    lease_token: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(String(100), nullable=True)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
