"""短事务登记事实，模型请求期间释放数据库连接，版本比较后再提交。"""

from app.domains.memory.scopes.models import ScopeMemory
import json
import threading
from uuid import uuid4

from sqlalchemy import select, update

from app.infrastructure.persistence.database import db_session
from app.domains.memory.errors import MemoryConflict, MemoryDeferred
from app.domains.memory.metrics import merge_metrics
from app.domains.memory.projections import _memory
from app.domains.memory.recording.facts import (
    record_evidence_fact,
    record_fact,
    record_message_fact,
    state_for,
)
from app.domains.memory.schemas import estimate_tokens
from app.infrastructure.persistence.defaults import now
from app.domains.assessment.models import Attempt
from app.domains.sessions.models import LearningSession, Message
from app.domains.assessment.models import EvidenceConceptLink
from app.domains.memory.models import (
    MemoryFact,
    MemoryJob,
)


from app.domains.memory.scopes.dependencies import ensure_scope_state


def _session(db, session_id: str) -> LearningSession:
    session = db.get(LearningSession, session_id, with_for_update=True)
    if session is None:
        raise ValueError("会话不存在")
    return session


class MemoryService:
    def __init__(self, models):
        self.worker = None
        self.business_pipeline = None
        self._capture_lock = threading.RLock()
        from app.domains.memory.scopes.service import HierarchicalMemoryService
        from app.domains.memory.concepts.service import BusinessMemoryPipeline

        BusinessMemoryPipeline(
            models,
            self,
            hierarchy=HierarchicalMemoryService(models, database=self.database),
        )
        self.configure_graph()

    def configure_graph(self, checkpointer=None, *, store=None):
        """生产生命周期显式注入PostgreSQL组件；恢复测试显式提供隔离Saver。"""
        from app.domains.memory.workflow import MemoryGraphRunner
        self.memory_graph = MemoryGraphRunner(self, checkpointer=checkpointer, store=store)
        return self.memory_graph

    def database(self):
        return db_session()

    @staticmethod
    def _mark_superseded(job):
        job.status, job.error, job.lease_until = "superseded", None, None

    @staticmethod
    def _is_superseded(db, job, state, experience) -> bool:
        # 结束版本过期优先处理，不能把旧 final 当作普通 provisional 重试。
        if job.final and (not state.closed or state.close_version != job.close_version
                          or state.next_seq != job.target_seq):
            return True
        if experience.experience_covered_seq < job.target_seq:
            return False
        statement = select(MemoryJob.id).where(
            MemoryJob.session_id == job.session_id,
            MemoryJob.extractor_version == job.extractor_version,
            MemoryJob.status == "completed", MemoryJob.target_seq >= job.target_seq,
        )
        if job.final:
            if experience.experience_status != "final":
                return False
            statement = statement.where(MemoryJob.final.is_(True), MemoryJob.close_version == job.close_version)
        if db.scalar(statement.limit(1)):
            return True
        # 明确遗忘经历将水位推进到已撤销事实；旧提取不能再回读这一前缀。
        return bool(
            experience.blocked_source_refs
            and experience.experience_covered_seq >= job.target_seq
        )

    def prepare_job(self, db, job) -> str:
        """在领取额度之前区分已覆盖任务和正常会话占用。"""
        if job.job_kind == "scope_aggregate":
            from app.domains.memory.scopes.promotion import AGGREGATION_RULE
            owner = db.get(ScopeMemory,job.scope_key)
            expected = self.business_pipeline.rule_version if owner and owner.scope_kind == "concept" else AGGREGATION_RULE
            if owner is None or job.rule_version != expected:
                self._mark_superseded(job)
                return "superseded"
            return "ready"
        if job.extractor_version != self.business_pipeline.rule_version:
            self._mark_superseded(job)
            return "superseded"
        session = _session(db, job.session_id)
        state = state_for(db, session.id)
        _, experience = self._rows(db, session)
        if self._is_superseded(db, job, state, experience):
            self._mark_superseded(job)
            return "superseded"
        if session.active_turn_id:
            return "deferred"
        return "ready"

    def _settle_covered_jobs(self, db, completed_job, state, experience):
        """较新任务成功后立即收敛同提取版本的旧任务，不等它们退避到期。"""
        db.flush()
        rows = db.scalars(select(MemoryJob).where(
            MemoryJob.session_id == completed_job.session_id,
            MemoryJob.extractor_version == completed_job.extractor_version,
            MemoryJob.id != completed_job.id,
            MemoryJob.status.in_(["pending", "retry", "failed"]),
        )).all()
        for row in rows:
            if self._is_superseded(db, row, state, experience):
                self._mark_superseded(row)

    @staticmethod
    def _rows(db, session):
        state = state_for(db, session.id)
        return ensure_scope_state(db, session.student_id, "student"), state

    @staticmethod
    def _scan(db, session):
        """补齐直接写消息的辅导入口及重启前未登记事实，不重新执行教学。"""
        known_refs = set(db.scalars(select(MemoryFact.source_ref).where(MemoryFact.session_id == session.id)))
        for message in db.scalars(select(Message).where(Message.session_id == session.id).order_by(Message.created_at, Message.id)):
            if "message:" + message.id in known_refs:
                continue
            record_message_fact(db, message)
        for attempt in db.scalars(select(Attempt).where(Attempt.session_id == session.id).order_by(Attempt.created_at, Attempt.id)):
            if "attempt:" + attempt.id in known_refs:
                continue
            record_fact(db, session.id, "attempt:" + attempt.id, "attempt", {
                "question_id": attempt.question_id, "answer_text": attempt.answer_text,
                "result": attempt.result, "score": attempt.score, "assisted_before_response": attempt.assisted,
                "assessment": attempt.assessment, "_source_created_at": attempt.created_at.isoformat(),
            })
        # 逐点评分归因带修订版本独立登记，不能把整题结果复制为每个概念的事实。
        for link in db.scalars(select(EvidenceConceptLink).where(EvidenceConceptLink.session_id == session.id,
                                                               EvidenceConceptLink.student_id == session.student_id)):
            ref = f"evidence_link:{link.id}:v{link.version}"
            if ref in known_refs:
                continue
            record_evidence_fact(db, link)

    def _enqueue(self, db, session, state, reason, final=False):
        extractor_version = self.business_pipeline.rule_version
        job = db.scalar(select(MemoryJob).where(MemoryJob.session_id == session.id,
            MemoryJob.target_seq == state.next_seq, MemoryJob.extractor_version == extractor_version,
            MemoryJob.close_version == state.close_version))
        if job is None:
            job = MemoryJob(id=str(uuid4()), session_id=session.id, target_seq=state.next_seq,
                            extractor_version=extractor_version, close_version=state.close_version,
                            final=final, reason=reason, status="pending", attempts=0, metrics={})
            db.add(job)
            db.flush()
        elif job.status == "failed" and reason in {"explicit_refresh", "close"}:
            # 人工显式刷新可恢复耗尽自动重试的任务，保留同一幂等键。
            job.status, job.attempts, job.error, job.available_at = "pending", 0, None, now()
        elif job.status == "retry" and reason in {"explicit_refresh", "close"}:
            job.available_at = now()
        state.last_enqueued_seq = max(state.last_enqueued_seq, state.next_seq)
        return job.id

    def capture(self, session_id: str, *, reason: str = "turn", force: bool = False, close: bool = False) -> dict:
        """仅登记已提交事实和任务，成功教学不等待提取模型。"""
        with self._capture_lock, self.database() as db:
            session = _session(db, session_id)
            self._scan(db, session)
            state = state_for(db, session_id)
            self._rows(db, session)
            if close:
                if session.active_turn_id:
                    raise ValueError("会话仍在处理业务事实，暂不能结束记忆")
                if not state.closed:
                    state.closed = True
                    state.close_version += 1
                    record_fact(db, session_id, "close:" + str(state.close_version), "lifecycle", {"closed": True}, reopen=False)
                force = True
            facts = list(db.scalars(select(MemoryFact).where(MemoryFact.session_id == session_id,
                                                            MemoryFact.seq > state.last_enqueued_seq)))
            rounds = sum(f.kind == "message" and f.payload.get("role") == "user" for f in facts)
            size = sum(estimate_tokens(json.dumps(f.payload, ensure_ascii=False)) for f in facts)
            threshold = rounds >= 5 or size >= 3000
            job_id = None
            # 中途输入已经持久化并不代表一次完整教学；后台不会抢跑处理当前轮。
            if (force or threshold) and not session.active_turn_id:
                job_id = self._enqueue(db, session, state, reason, final=close or state.closed)
            return {"session_id": session_id, "target_seq": state.next_seq, "job_id": job_id,
                    "status": "queued" if job_id else "recorded", "new_fact_tokens": size, "complete_turns": rounds}

    async def refresh(self, session_id: str, *, close: bool = False, directive: str | None = None, wait: bool = True) -> dict:
        if directive:
            return await self.correct(session_id, directive, wait=wait)
        result = self.capture(session_id, reason="close" if close else "explicit_refresh", force=True, close=close)
        if wait and result["job_id"]:
            from app.domains.memory.recording.jobs import MemoryWorker
            worker = self.worker or MemoryWorker(self)
            await worker.run_once(job_id=result["job_id"])
            return {**result, **self.read(session_id)}
        return result

    def read(self, session_id):
        return self.business_pipeline.read(session_id)

    def context_for_scope(
        self,
        student_id,
        course_id,
        concept_ids=None,
        limit=3,
        exclude_session_id=None,
        **kwargs,
    ):
        return self.business_pipeline.context_for_scope(
            student_id, course_id, concept_ids, limit, exclude_session_id, **kwargs
        )

    def context_for_session(self, session_id, concept_ids=None, limit=3, **kwargs):
        return self.business_pipeline.context_for_session(
            session_id, concept_ids, limit, **kwargs
        )

    def forget(self, session_id, kind="student"):
        if kind not in {"student", "experience", "all"}:
            raise ValueError("未知记忆类型")
        if kind in {"student", "all"}:
            self.business_pipeline.hierarchy.forget(session_id, "student")
        if kind in {"experience", "all"}:
            with self._capture_lock, self.database() as db:
                session = _session(db, session_id)
                self._scan(db, session)
                extraction = state_for(db, session_id)
                refs = list(db.scalars(select(MemoryFact.source_ref).where(MemoryFact.session_id == session_id)))
                extraction.blocked_source_refs = sorted(
                    set(extraction.blocked_source_refs or []) | set(refs)
                )
                (
                    extraction.content,
                    extraction.experience_source_refs,
                    extraction.concept_tags,
                ) = ("", [], [])
                extraction.experience_covered_seq = extraction.next_seq
                extraction.experience_version += 1
                extraction.invalidation_version += 1
        return self.read(session_id)

    async def correct(self, session_id, text, *, kind="student", wait=True):
        if kind == "student":
            return await self.business_pipeline.hierarchy.correct(
                session_id, text, wait=wait
            )
        if not text.strip() or estimate_tokens(text) > 2000:
            raise ValueError("纠正内容必须非空且不超过2000 Tokens")
        self.forget(session_id, kind)
        with self.database() as db:
            record_fact(db, session_id, "correction:" + str(uuid4()), "correction",
                        {"instruction": text.strip(), "kind": kind, "explicit_student_request": True})
        return await self.refresh(session_id, wait=wait)

    async def process(self, job_id):
        with self.database() as db:
            job = db.get(MemoryJob, job_id)
            kind, key = job.job_kind, job.scope_key
        if kind == "session_extract":
            return await self.business_pipeline.process(job_id)
        with self.database() as db:
            row = db.get(ScopeMemory, key)
            owner = (row.student_id, row.scope_kind, row.course_id, row.concept_id)
        if owner[1] == "concept":
            return await self.business_pipeline.aggregate_concept(
                owner[0], owner[2], owner[3]
            )
        return await self.business_pipeline.hierarchy._aggregate_scope(
            owner[0], owner[1], owner[2]
        )

    def recover(self):
        """统一队列恢复：只恢复已有任务，旧规则结清，不扫描历史生成新工作。"""
        from app.domains.memory.scopes.promotion import AGGREGATION_RULE
        recovered = superseded = 0
        with self.database() as db:
            for job in db.scalars(select(MemoryJob).where(MemoryJob.status.in_(["pending","retry","running"]))):
                stale = (job.extractor_version != self.business_pipeline.rule_version if job.job_kind == "session_extract"
                    else job.rule_version not in {self.business_pipeline.rule_version,AGGREGATION_RULE})
                if stale:
                    self._mark_superseded(job)
                    superseded += int(job.job_kind == "scope_aggregate")
                elif job.status == "running":
                    recovered += int(job.job_kind == "scope_aggregate")
                    job.status,job.lease_until,job.available_at = "retry",None,now()
        return {"sessions":0,"scope_jobs_recovered":recovered,"scope_jobs_superseded":superseded}
