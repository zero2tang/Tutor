"""业务事实留在主库；向量索引可以从这些版本重新构建。"""

from datetime import datetime
from sqlalchemy import DateTime, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.infrastructure.persistence.database import Base
from app.infrastructure.persistence.defaults import now


class QuestionAsset(Base):
    __tablename__ = "exercise_search_assets"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    question_id: Mapped[str] = mapped_column(String(96), index=True)
    course_id: Mapped[str] = mapped_column(String(60), index=True)
    role: Mapped[str] = mapped_column(String(24))
    content_hash: Mapped[str] = mapped_column(String(64))
    location: Mapped[str] = mapped_column(Text)
    source: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="ready")


class QuestionRepresentation(Base):
    __tablename__ = "exercise_search_representations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    question_id: Mapped[str] = mapped_column(String(96), index=True)
    course_id: Mapped[str] = mapped_column(String(60), index=True)
    source_hash: Mapped[str] = mapped_column(String(64))
    asset_ids: Mapped[list] = mapped_column(JSON)
    extractor: Mapped[str] = mapped_column(String(180))
    payload: Mapped[dict] = mapped_column(JSON)
    review_status: Mapped[str] = mapped_column(String(24), default="pending")
    review: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class QuestionSearchProjection(Base):
    __tablename__ = "exercise_search_projections"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    question_id: Mapped[str] = mapped_column(String(96), index=True)
    course_id: Mapped[str] = mapped_column(String(60), index=True)
    representation_id: Mapped[str] = mapped_column(String(64))
    source_hash: Mapped[str] = mapped_column(String(64))
    generation: Mapped[str] = mapped_column(String(64), index=True)
    config_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default="ready", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


