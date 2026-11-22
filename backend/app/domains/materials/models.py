"""教材原件、解析快照和教学分析包；检索投影不能替代来源事实。"""

from datetime import datetime
from sqlalchemy import DateTime, Integer, JSON, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, synonym
from app.infrastructure.persistence.database import Base
from app.infrastructure.persistence.defaults import now, uid



class TextbookDocument(Base):
    __tablename__ = "textbook_documents"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    course_id: Mapped[str] = mapped_column(String(60), index=True)
    title: Mapped[str] = mapped_column(String(250))
    edition: Mapped[str] = mapped_column(String(120), default="")
    source_uri: Mapped[str] = mapped_column(Text)
    source_kind: Mapped[str] = mapped_column(String(40))
    format: Mapped[str] = mapped_column(String(16))
    content_hash: Mapped[str] = mapped_column(String(64))
    raw_content: Mapped[bytes] = mapped_column(LargeBinary(length=16777215))
    parser_version: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(32))
    parsed: Mapped[dict] = mapped_column(JSON)
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    # PDF 原件与页图信息；解析快照继续使用原 ID，不按原始哈希覆盖。
    asset_root: Mapped[str] = mapped_column(Text, default="")
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    source_hash = synonym("content_hash")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class TextbookImportReceipt(Base):
    __tablename__ = "textbook_import_receipts"
    __table_args__ = (
        UniqueConstraint("course_id", "request_key", name="uq_textbook_import_request"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    course_id: Mapped[str] = mapped_column(String(60))
    request_key: Mapped[str] = mapped_column(String(80))
    request_hash: Mapped[str] = mapped_column(String(64))
    document_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
