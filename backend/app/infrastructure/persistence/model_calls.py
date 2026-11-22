"""教材分页和题目图文调用共享的薄审计记录；不承担工作流。"""

from datetime import datetime
from sqlalchemy import select, DateTime, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.infrastructure.persistence.database import Base
from app.infrastructure.persistence.defaults import now


class ModelCall(Base):
    __tablename__ = "model_calls"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(40), index=True)
    resource_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    stage: Mapped[str] = mapped_column(String(32))
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_hash: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(24))
    response: Mapped[dict] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


def completed_call(db, purpose, request_hash):
    # 只排序轻量 ID，失败回执与不同业务目的均不能命中。
    identifier = db.scalar(select(ModelCall.id).where(ModelCall.purpose == purpose,
        ModelCall.request_hash == request_hash, ModelCall.status == "completed")
        .order_by(ModelCall.created_at.desc(), ModelCall.id.desc()).limit(1))
    return db.get(ModelCall, identifier) if identifier else None
