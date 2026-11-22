"""单进程后台 worker：数据库持久任务、租约、有限退避和启动恢复。"""

from app.domains.memory.scopes.models import SessionMemoryExtraction
import asyncio
from datetime import timedelta
import logging

from sqlalchemy import select, update, and_, or_

from app.infrastructure.persistence.defaults import now
from app.domains.sessions.models import LearningSession
from app.domains.memory.models import MemoryJob

logger = logging.getLogger(__name__)


def enqueue_scope(db, scope, rule_version):
    """只登记下一层唤醒；模型候选及节点进度由LangGraph检查点保存。"""
    if not scope.enabled:
        return None
    existing = db.scalar(select(MemoryJob).where(
        MemoryJob.job_kind == "scope_aggregate", MemoryJob.scope_key == scope.scope_key,
        MemoryJob.source_generation == scope.generation, MemoryJob.rule_version == rule_version))
    if existing is not None:
        return existing
    job = MemoryJob(job_kind="scope_aggregate", scope_key=scope.scope_key,
        source_generation=scope.generation, rule_version=rule_version,
        status="pending", input_fingerprint="", result_snapshot={})
    db.add(job)
    db.flush()
    return job


class MemoryWorker:
    def __init__(self, service, *, poll_seconds: float = 2.0):
        self.service = service
        self.poll_seconds = poll_seconds
        self._lock = asyncio.Lock()
        self._task = None
        self._stopping = asyncio.Event()
        self._last_idle_sweep = None
        service.worker = self

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self.service.recover()
        self._stopping.clear()
        self._task = asyncio.create_task(self._poll(), name="model-memory-worker")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _poll(self):
        while not self._stopping.is_set():
            try:
                worked = await self.run_once()
                self._idle_sweep()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("memory_worker_iteration_failed type=%s", type(exc).__name__)
                worked = False
            if not worked:
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=self.poll_seconds)
                except TimeoutError:
                    pass

    def _idle_sweep(self):
        current = now()
        if self._last_idle_sweep and current - self._last_idle_sweep < timedelta(minutes=1):
            return
        self._last_idle_sweep = current
        with self.service.database() as db:
            ids = list(
                db.scalars(
                    select(SessionMemoryExtraction.session_id)
                    .join(
                        LearningSession,
                        LearningSession.id == SessionMemoryExtraction.session_id,
                    )
                    .where(
                        SessionMemoryExtraction.next_seq
                        > SessionMemoryExtraction.last_enqueued_seq,
                        SessionMemoryExtraction.updated_at
                        < current - timedelta(minutes=30),
                        LearningSession.active_turn_id.is_(None),
                    )
                )
            )
        for session_id in ids:
            self.service.capture(session_id, reason="idle_provisional", force=True)

    async def run_once(self, *, job_id: str | None = None) -> bool:
        """同一服务的手动刷新与轮询共享锁，不会重复消费同一任务。"""
        async with self._lock:
            with self.service.database() as db:
                statement = select(MemoryJob).where(
                    or_(
                        and_(
                            MemoryJob.status.in_(["pending", "retry"]),
                            MemoryJob.available_at <= now(),
                        ),
                        and_(
                            MemoryJob.status == "running",
                            MemoryJob.lease_until <= now(),
                        ),
                    )
                )
                if job_id:
                    statement = statement.where(MemoryJob.id == job_id)
                job = db.scalar(statement.order_by(MemoryJob.created_at).limit(1))
                if job is None:
                    return False
                disposition = self.service.prepare_job(db, job)
                if disposition == "superseded":
                    return True
                if disposition == "deferred":
                    self._defer(job)
                    return True
                if job.job_kind == "session_extract" and job.attempts >= 5:
                    # 最后一次尝试若在进程崩溃时中断，恢复不能重新获得第六次额度。
                    job.status, job.error, job.lease_until = "failed", "MemoryAttemptBudgetExceeded", None
                    return True
                identifier, old_status = job.id, job.status
                kind, token = job.job_kind, None
                if job.job_kind == "session_extract":
                    from uuid import uuid4

                    token = uuid4().hex
                    claimed = db.execute(
                        update(MemoryJob)
                        .where(
                            MemoryJob.id == identifier, MemoryJob.status == old_status,
                            MemoryJob.attempts == job.attempts, MemoryJob.attempts < 5,
                        )
                        .values(
                            status="running",
                            attempts=job.attempts + 1,
                            lease_token=token,
                            lease_until=now() + timedelta(minutes=15),
                        )
                    )
                    if claimed.rowcount != 1:
                        return False
            try:
                extractor = self.service.business_pipeline.extractor
                extractor.last_metrics = None
                await self.service.process(identifier)
            except asyncio.CancelledError:
                with self.service.database() as db:
                    row = db.get(MemoryJob, identifier)
                    if (
                        kind == "session_extract"
                        and row.lease_token == token
                        and row.status == "running"
                    ):
                        row.status, row.lease_until, row.available_at = "retry", None, now()
                raise
            except Exception as exc:
                from app.domains.memory.errors import MemoryDeferred
                from app.domains.memory.metrics import merge_metrics

                with self.service.database() as db:
                    row = db.get(MemoryJob, identifier)
                    # 聚合层自行处理租约和错误；外层不得覆盖其新领取的任务。
                    if (
                        kind != "session_extract"
                        or row.lease_token != token
                        or row.status != "running"
                    ):
                        return True
                    if isinstance(exc, MemoryDeferred):
                        # 领取后下一轮恰好开始：退回尚未发生模型调用的领取额度。
                        row.attempts = max(0, row.attempts - 1)
                        self._defer(row)
                        return True
                    # 只保存异常类型，模型正文、学生输入、凭据均不进入错误日志。
                    row.status = "retry" if row.attempts < 5 else "failed"
                    row.error, row.lease_until = type(exc).__name__, None
                    row.available_at = now() + timedelta(seconds=min(300, 2 ** row.attempts))
                    extractor = self.service.business_pipeline.extractor
                    if row.job_kind == "session_extract":
                        row.metrics = merge_metrics(row.metrics, self.service.memory_graph.failure_metrics(identifier))
                        row.result_snapshot = {**row.result_snapshot,
                            "graph_usage_accounted": self.service.memory_graph.failure_accounting(identifier)}
            return True

    def _defer(self, job):
        """忙碌属于调度等待，不写模型错误，也不消耗有限重试额度。"""
        job.status, job.error, job.lease_until = "pending", None, None
        job.available_at = now() + timedelta(seconds=max(1, self.poll_seconds))
        job.metrics = {**(job.metrics or {}), "deferrals": (job.metrics or {}).get("deferrals", 0) + 1}
