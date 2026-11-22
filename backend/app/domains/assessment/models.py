"""该业务功能拥有的持久化记录；表名、字段与约束保持原有契约。"""

from app.infrastructure.persistence.database import Base
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime
from app.infrastructure.persistence.defaults import now, uid

PRECISE_TIME = DateTime().with_variant(DATETIME(fsp=6), "mysql")


class EvidenceConceptLink(Base):
    __tablename__ = "evidence_concept_links"

    # 稳定键不含来源版本。重评修订同一贡献，不能再累加一份。
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    student_id: Mapped[str] = mapped_column(String(60), index=True)
    course_id: Mapped[str] = mapped_column(String(60), index=True)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    source_ref: Mapped[str] = mapped_column(String(160), index=True)
    source_version: Mapped[str] = mapped_column(String(96))
    evidence_group_id: Mapped[str] = mapped_column(String(160), index=True)
    canonical_item_id: Mapped[str] = mapped_column(String(160), index=True)
    concept_id: Mapped[str] = mapped_column(String(96), index=True)
    dimension: Mapped[str] = mapped_column(String(32))
    criterion_id: Mapped[str] = mapped_column(String(96))
    source_kind: Mapped[str] = mapped_column(String(32), default="graded_attempt")
    attribution: Mapped[str] = mapped_column(String(24))
    observed_result: Mapped[str] = mapped_column(String(24))
    source_span_refs: Mapped[list] = mapped_column(JSON, default=list)
    help_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    eligible_for_mastery: Mapped[bool] = mapped_column(Boolean, default=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(PRECISE_TIME, default=now)
    updated_at: Mapped[datetime] = mapped_column(
        PRECISE_TIME, default=now, onupdate=now
    )


class BatchSubmission(Base):
    __tablename__ = "batch_submissions"
    __table_args__ = (
        UniqueConstraint("session_id", "idempotency_key", name="uq_batch_submission"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    batch_id: Mapped[str] = mapped_column(String(36))
    idempotency_key: Mapped[str] = mapped_column(String(80))
    payload_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Attempt(Base):
    __tablename__ = "attempts"
    __table_args__ = (
        UniqueConstraint("session_id", "idempotency_key", name="uq_attempt_request"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    question_id: Mapped[str] = mapped_column(String(96), index=True)
    answer_text: Mapped[str] = mapped_column(Text)
    result: Mapped[str] = mapped_column(String(24))
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    assisted: Mapped[bool] = mapped_column(Boolean, default=False)
    assessment: Mapped[dict] = mapped_column(JSON)
    state_updates: Mapped[list] = mapped_column(JSON, default=list)
    idempotency_key: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
