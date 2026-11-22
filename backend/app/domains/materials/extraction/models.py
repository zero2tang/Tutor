"""按小节保存不可变提取版本、任务租约与原页复核。"""

from datetime import datetime
from sqlalchemy import DateTime, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, synonym
from app.infrastructure.persistence.database import Base
from app.infrastructure.persistence.defaults import now


class ExtractedSection(Base):
    __tablename__ = "textbook_extraction_sections"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    book_id: Mapped[str] = mapped_column(String(64), index=True)
    number: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(250))
    page_start: Mapped[int] = mapped_column(Integer)
    page_end: Mapped[int] = mapped_column(Integer)
    review_status: Mapped[str] = mapped_column(String(32), default="pending")
    status = synonym("review_status")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    payload_hash: Mapped[str] = mapped_column(String(64), default="")
    review: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


    # 同一小节版本拥有提取状态与复核状态，二者不可混为一谈。
    fingerprint: Mapped[str] = mapped_column(String(64), default="")
    extraction_status: Mapped[str] = mapped_column(String(32), default="pending")
    lease_token: Mapped[str] = mapped_column(String(64), default="")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    receipt: Mapped[dict] = mapped_column(JSON, default=dict)
