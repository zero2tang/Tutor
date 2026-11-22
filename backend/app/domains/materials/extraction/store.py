"""短事务领取任务、保存不可变结果；失败后复用已完成的分页调用。"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4
from copy import deepcopy
from sqlalchemy import select, update, or_
from app.infrastructure.persistence.database import db_session
from app.infrastructure.documents.pdf import digest
from app.domains.courses.models import Course, Concept, Material, Question
from .models import ExtractedSection
from app.domains.materials.models import TextbookDocument
from app.infrastructure.persistence.model_calls import ModelCall, completed_call
from .catalog import publish, withdraw


class ExtractionStore:
    def __init__(self, session=db_session):
        self.session = session

    def register(self, manifest, root, raw, course_id, title):
        book_id = digest([course_id, manifest["source_hash"]])
        with self.session() as db:
            if not db.get(Course, course_id):
                db.add(Course(id=course_id, title=title, description="教材原文与有来源的教学资源；模型归纳单独标注。", source_ref="user_provided_textbook"))
            if not db.get(TextbookDocument, book_id):
                db.add(TextbookDocument(id=book_id, course_id=course_id, title=title,
                    content_hash=manifest["source_hash"], raw_content=raw, asset_root=str(root),
                    edition="", source_uri=manifest.get("source_path", ""), source_kind="user_provided_textbook",
                    format="pdf", parser_version="multimodal-v1", status="registered", parsed={}, provenance={},
                    page_count=manifest["page_count"], manifest=manifest))
            else:
                db.get(TextbookDocument, book_id).manifest = manifest
            return book_id

    def book(self, book_id):
        with self.session() as db:
            row = db.get(TextbookDocument, book_id)
            if row is None or row.format != "pdf" or not row.page_count:
                raise ValueError("教材 PDF 原件不存在。")
            return row

    def create_job(self, book_id, section, fingerprint):
        identifier = digest([book_id, section, fingerprint])
        with self.session() as db:
            if not db.get(ExtractedSection, identifier):
                db.add(ExtractedSection(id=identifier, book_id=book_id, number=section["number"], title=section["title"],
                    page_start=section["page_start"], page_end=section["page_end"], review_status="pending", fingerprint=fingerprint))
        return identifier

    def claim(self, job_id):
        token = uuid4().hex
        current = datetime.now(timezone.utc).replace(tzinfo=None)
        with self.session() as db:
            row = db.get(ExtractedSection, job_id)
            if row.extraction_status == "completed":
                return None
            result = db.execute(update(ExtractedSection).where(ExtractedSection.id == job_id,
                ExtractedSection.extraction_status != "completed", or_(ExtractedSection.lease_until.is_(None), ExtractedSection.lease_until < current))
                .values(extraction_status="running", lease_token=token, lease_until=current + timedelta(minutes=30),
                        attempts=ExtractedSection.attempts + 1, error=""))
            if result.rowcount != 1:
                raise ValueError("该提取任务已有有效运行租约，请稍后恢复。")
        return token

    def call_started(self, job_id, stage, page, model, request_hash):
        identifier = uuid4().hex
        with self.session() as db:
            db.add(ModelCall(id=identifier, purpose="textbook", resource_id=job_id, stage=stage, page=page, status="running", model=model, request_hash=request_hash))
        return identifier

    def call_finished(self, identifier, *, response=None, metrics=None, error="", status="completed"):
        with self.session() as db:
            row = db.get(ModelCall, identifier)
            row.status, row.error = status, error
            row.response, row.metrics = response or {}, metrics or {}

    def cached_call(self, job_id, request_hash):
        with self.session() as db:
            row = completed_call(db, "textbook", request_hash)
            return {**row.response, "cache_source_id": row.id, "cache_source_job": row.resource_id} if row else None

    def truncated_call(self, request_hash):
        with self.session() as db:
            ids = db.scalars(select(ModelCall.id).where(ModelCall.purpose == "textbook", ModelCall.request_hash == request_hash,
                ModelCall.status == "failed").order_by(ModelCall.created_at.desc())).all()
            rows = [db.get(ModelCall, key) for key in ids]
            return next((row for row in rows if any(c.get("finish_reason") == "length" for c in row.response.get("raw", {}).get("choices", []))), None)

    def fail(self, job_id, token, error):
        with self.session() as db:
            db.execute(update(ExtractedSection).where(ExtractedSection.id == job_id, ExtractedSection.lease_token == token)
                .values(extraction_status="failed", error=error[:1000], lease_token="", lease_until=None))

    def calls(self, job_id):
        with self.session() as db:
            ids = db.scalars(select(ModelCall.id).where(ModelCall.purpose == "textbook", ModelCall.resource_id == job_id).order_by(ModelCall.created_at)).all()
            return [db.get(ModelCall, key) for key in ids]

    def job(self, identifier):
        with self.session() as db:
            row = db.get(ExtractedSection, identifier)
            if row is None:
                raise ValueError("提取任务不存在。")
            return row

    def commit(self, job_id, token, payload, receipt):
        with self.session() as db:
            job = db.get(ExtractedSection, job_id, with_for_update=True)
            if job is None or job.lease_token != token or job.extraction_status != "running" or job.lease_until is None or job.lease_until < datetime.now(timezone.utc).replace(tzinfo=None):
                raise ValueError("提取租约已失效，拒绝发布。")
            section = job
            section.payload, section.payload_hash, section.status = payload, digest(payload), "extracted"
            job.extraction_status, job.receipt = "completed", receipt
            job.lease_token, job.lease_until = "", None

    def section(self, identifier):
        with self.session() as db:
            row = db.get(ExtractedSection, identifier)
            if row is None or not row.payload_hash or digest(row.payload) != row.payload_hash:
                raise ValueError("小节不存在、未完成或存储指纹不一致。")
            return row

    def review(self, identifier, result):
        self.section(identifier)
        with self.session() as db:
            row = db.get(ExtractedSection, identifier)
            if result.get("payload_hash") != row.payload_hash:
                raise ValueError("复核回执的内容指纹不对应当前小节版本。")
            if not result.get("reviewer") or "passed" not in result:
                raise ValueError("复核回执必须说明复核人及结论。")
            row.review = result
            row.status = "reviewed" if result["passed"] else "needs_revision"
            if result["passed"]:
                publish(db, db.get(TextbookDocument, row.book_id), row)
            else:
                withdraw(db, row)

    def sections(self, book_id):
        with self.session() as db:
            # 先排序小字段ID，避免MySQL把大JSON放进filesort耗尽排序缓冲。
            ids = db.scalars(select(ExtractedSection.id).where(ExtractedSection.book_id == book_id)
                .order_by(ExtractedSection.page_start, ExtractedSection.updated_at.desc())).all()
            return [db.get(ExtractedSection, key) for key in ids]

    def amend(self, identifier, patches, reviewer):
        """原页复核的局部更正生成新快照，不改写模型原始回执或旧结果。"""
        original = self.section(identifier)
        payload = deepcopy(original.payload)
        for patch in patches:
            path = patch["path"]
            if not path or path[0] not in {"definitions", "exercises", "methods", "figures", "teaching_design"} or not patch.get("source_refs"):
                raise ValueError("原页更正仅允许修改三类资源并附来源。")
            if any(type(ref.get("pdf_page")) is not int or not original.page_start <= ref["pdf_page"] <= original.page_end for ref in patch["source_refs"]):
                raise ValueError("原页更正的来源必须属于当前小节。")
            target = payload
            for key in path[:-1]:
                target = target[key]
            if target[path[-1]] != patch["before"]:
                raise ValueError("原页更正的原值已变化，拒绝覆盖。")
            target[path[-1]] = deepcopy(patch["value"])
        with self.session() as db:
            source_job = db.get(ExtractedSection, identifier)
            fingerprint = source_job.fingerprint
        revision_id = digest([identifier, patches, reviewer])
        payload["provenance"] = {**payload["provenance"], "corrected_from": identifier,
            "pipeline_fingerprint": fingerprint, "source_review_corrections": patches}
        job_id = self.create_job(original.book_id, {"number": original.number, "title": original.title,
            "page_start": original.page_start, "page_end": original.page_end, "revision": revision_id}, fingerprint)
        token = self.claim(job_id)
        if token:
            self.commit(job_id, token, payload, {"source_job_id": identifier, "model_calls": 0,
                "reviewer": reviewer, "corrections": len(patches), "note": "复核更正不冒充重新调用模型。"})
        return job_id
