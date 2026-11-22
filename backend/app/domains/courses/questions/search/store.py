"""短事务保存版本、调用回执与复核；模型和 ES 请求不占数据库事务。"""

from uuid import uuid4
from datetime import timedelta
from sqlalchemy import select, update, or_
from sqlalchemy.exc import IntegrityError
from app.infrastructure.persistence.database import db_session
from app.domains.courses.models import Question, IndexTask
from app.infrastructure.persistence.model_calls import ModelCall, completed_call
from app.domains.courses.questions.policy import can_search
from app.infrastructure.persistence.defaults import now
from .models import QuestionAsset, QuestionRepresentation, QuestionSearchProjection
from .representation import public_snapshot, is_public, digest, source_hash, validate_extraction


class QuestionSearchStore:
    def __init__(self, database=db_session):
        self.database = database

    def claim_build(self, course_id, namespace):
        identifier = digest([course_id, namespace, "question_search"])
        self.insert(IndexTask, {"id": identifier, "course_id": course_id, "namespace": namespace, "target": "question_search", "status": "pending", "token": "", "details": {}})
        token = uuid4().hex
        with self.database() as db:
            changed = db.execute(update(IndexTask).where(IndexTask.id == identifier,
                or_(IndexTask.lease_until.is_(None), IndexTask.lease_until < now()))
                .values(status="running", token=token, lease_until=now() + timedelta(minutes=30)))
            if changed.rowcount != 1:
                raise ValueError("该课程索引已有有效构建租约")
        return identifier, token

    def renew_build(self, identifier, token, *, finish=False, receipt=None):
        with self.database() as db:
            changed = db.execute(update(IndexTask).where(IndexTask.id == identifier,
                IndexTask.token == token, IndexTask.lease_until > now())
                .values(lease_until=None if finish else now() + timedelta(minutes=30), status=("failed" if (receipt or {}).get("status") == "failed" else "completed") if finish else "running", details=receipt or {}))
            if changed.rowcount != 1:
                raise ValueError("构建租约已过期或被其他工作进程接管")

    def question(self, identifier, course_id, *, include_withdrawn=False):
        with self.database() as db:
            row = db.get(Question, identifier)
            if row is None or row.course_id != course_id or (not include_withdrawn and not can_search(row)):
                raise ValueError("题目不存在、已撤回或不属于当前课程")
            return row

    def questions(self, course_id):
        with self.database() as db:
            return [q for q in db.scalars(select(Question).where(Question.course_id == course_id).order_by(Question.id)).all() if can_search(q)]

    def insert(self, model, value):
        try:
            with self.database() as db:
                if not db.get(model, value["id"]):
                    db.add(model(**value))
        except IntegrityError:
            # 相同不可变版本的并发提交只保留一份；其他约束错误继续抛出。
            with self.database() as db:
                if db.get(model, value["id"]) is None:
                    raise

    def assets(self, identifier):
        with self.database() as db:
            return list(db.scalars(select(QuestionAsset).where(QuestionAsset.question_id == identifier).order_by(QuestionAsset.id)).all())

    def asset(self, identifier):
        with self.database() as db:
            return db.get(QuestionAsset, identifier)

    def representation(self, identifier):
        with self.database() as db:
            return db.get(QuestionRepresentation, identifier)

    def review(self, identifier, payload, *, reviewer, notes, expected_hash):
        if not reviewer.strip() or not notes.strip():
            raise ValueError("复核必须记录审阅者和原图核对说明")
        from .schemas import VisualExtraction
        value = VisualExtraction.model_validate(payload).model_dump()
        with self.database() as db:
            original = db.get(QuestionRepresentation, identifier)
            if original is None or original.source_hash != expected_hash:
                raise ValueError("复核版本已变化")
            q = db.get(Question, original.question_id)
            assets = [db.get(QuestionAsset, a) for a in original.asset_ids]
            if q is None or any(a is None for a in assets):
                raise ValueError("题面或资产已删除，不能复核旧版本")
            snapshot = public_snapshot(q)
            if not is_public(snapshot) or source_hash(snapshot, [asset_dict(a) for a in assets]) != expected_hash:
                raise ValueError("题面已变化或已撤回")
            validate_extraction(value, [asset_dict(a) for a in assets], snapshot)
            review = {"reviewer": reviewer, "notes": notes, "parent_id": original.id}
            version = digest([original.id, value, review])
            if not db.get(QuestionRepresentation, version):
                db.add(QuestionRepresentation(id=version, question_id=q.id, course_id=q.course_id, source_hash=expected_hash,
                    asset_ids=original.asset_ids, extractor=original.extractor, payload=value, review_status="approved", review=review))
            return version

    def selected_representation(self, question_id, fingerprint):
        with self.database() as db:
            return db.scalar(select(QuestionRepresentation).where(QuestionRepresentation.question_id == question_id,
                QuestionRepresentation.source_hash == fingerprint, QuestionRepresentation.review_status == "approved")
                .order_by(QuestionRepresentation.created_at.desc(), QuestionRepresentation.id))

    def cached_call(self, request_hash):
        with self.database() as db:
            return completed_call(db, "question_search", request_hash)

    def call(self, request_hash, stage, response, metrics, status):
        self.insert(ModelCall, {"id": uuid4().hex, "purpose": "question_search", "resource_id": request_hash, "request_hash": request_hash,
            "model": metrics.get("model", response.get("model", "")), "error": metrics.get("error", ""),
            "stage": stage, "response": response, "metrics": metrics, "status": status})

    def projections(self, course_id, *, published=True, namespace="main"):
        with self.database() as db:
            stmt = select(QuestionSearchProjection).where(QuestionSearchProjection.course_id == course_id)
            if published:
                stmt = stmt.where(QuestionSearchProjection.status == "published")
            return [p for p in db.scalars(stmt).all() if p.payload.get("namespace", "main") == namespace]

    def publish(self, course_id, generation, namespace="main", *, lease=None):
        with self.database() as db:
            if lease:
                task = db.get(IndexTask, lease[0], with_for_update=True)
                if (task is None or task.course_id != course_id or task.namespace != namespace
                        or task.target != "question_search" or task.token != lease[1]
                        or task.lease_until is None or task.lease_until <= now()):
                    raise ValueError("构建租约已失效，拒绝发布投影")
            rows = db.scalars(select(QuestionSearchProjection).where(QuestionSearchProjection.course_id == course_id)).all()
            for row in rows:
                if row.payload.get("namespace", "main") != namespace:
                    continue
                if row.generation == generation:
                    row.status = "published"
                elif row.status == "published":
                    row.status = "superseded"

    def withdraw(self, identifier, course_id):
        with self.database() as db:
            q = db.get(Question, identifier)
            if not q or q.course_id != course_id:
                raise ValueError("题目不属于当前课程")
            q.validation = {**(q.validation or {}), "visibility": "private"}
            for row in db.scalars(select(QuestionSearchProjection).where(QuestionSearchProjection.question_id == identifier)):
                row.status = "withdrawn"

    def receipt(self, course_id, session_id, payload):
        identifier = uuid4().hex
        from app.infrastructure.persistence.retrieval_log import record
        record(identifier, course_id, session_id, payload)
        return identifier


def asset_dict(row):
    return {key: getattr(row, key) for key in ("id", "question_id", "course_id", "role", "content_hash", "location", "source", "status")}
