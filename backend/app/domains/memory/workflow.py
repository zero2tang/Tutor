"""记忆任务的原生检查点边界；持久调度与业务提交仍有独立责任。"""

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from typing import TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.runtime import Runtime
from langgraph.types import RetryPolicy
from psycopg import OperationalError
from psycopg_pool import PoolTimeout

from app.domains.memory.scopes.promotion import (
    empty_metrics, generate_candidate, validate_candidate, review_candidate,
)
from app.domains.memory.models import MemoryJob


class MemoryState(TypedDict, total=False):
    job_id: str
    kind: str
    snapshot: dict
    candidate: dict | list | None
    validated: dict | None
    audit: dict
    metrics: dict
    outcome: dict


@dataclass
class MemoryRuntime:
    runner: object
    lease_token: str


def _identity(snapshot):
    """租约可重新领取；来源、正文版本和撤销屏障变化必须重新冻结。"""
    value = {key: item for key, item in snapshot.items()
             if key not in {"lease_token", "metrics", "validation_failures"}}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        default=lambda item: sorted(item) if isinstance(item, set) else str(item)).encode()).hexdigest()


def _bound_snapshot(state, runtime):
    return {**deepcopy(state["snapshot"]), "lease_token": runtime.context.lease_token}


def build_memory_graph(*, checkpointer, store=None):
    async def freeze(state, runtime: Runtime[MemoryRuntime]):
        # 输入来自同一轮短事务的冻结来源；在调用模型前由框架持久化。
        return {"snapshot": state["snapshot"]}

    async def generate(state, runtime: Runtime[MemoryRuntime]):
        runner, snapshot = runtime.context.runner, deepcopy(state["snapshot"])
        if state["kind"] == "extract":
            try:
                candidate, metrics = await runner.pipeline.generate_extraction(snapshot)
            finally:
                runner._failure_metrics[state["job_id"]] = runner.pipeline.extractor.metrics_snapshot() or {}
        elif state["kind"] == "concept":
            try:
                candidate, metrics = await runner.pipeline._aggregate(snapshot, snapshot["course_id"], snapshot["concept_id"])
            finally:
                runner._failure_metrics[state["job_id"]] = snapshot.get("metrics", {})
                runner._failure_audits[state["job_id"]] = snapshot.get("validation_failures", [])
        else:
            metrics = empty_metrics()
            try:
                candidate = await generate_candidate(runner.hierarchy.models, snapshot["model"], metrics) if snapshot["model"]["facts"] else {"no_change": True, "claims": []}
            finally:
                runner._failure_metrics[state["job_id"]] = metrics
        return {"candidate": candidate, "metrics": metrics, "snapshot": snapshot}

    async def validate(state, runtime: Runtime[MemoryRuntime]):
        if state["kind"] == "scope":
            if not state["snapshot"]["model"]["facts"]:
                return {"validated": {"no_change": True, "approved": [], "review": [], "rejected": [], "candidates": []}}
            return {"validated": validate_candidate(state["candidate"], state["snapshot"]["model"])}
        # 叶子及知识点的有限修复沿用原领域校验；这里核验最终持久候选仍绑定冻结来源。
        snapshot, candidate = state["snapshot"], state["candidate"]
        if state["kind"] == "extract":
            allowed = {item["source_ref"] for item in snapshot["all_facts"]}
            claims = [claim for item in candidate["observations"] for claim in item["claims"]]
            refs = {ref for item in candidate["preferences"] for ref in item["source_refs"]}
            refs.update(candidate["experience"].get("source_refs", []))
        else:
            source_claims = [*snapshot["old_claims"], *[claim for item in snapshot["inputs"] for claim in item["claims"]]]
            allowed = {ref for item in source_claims for ref in [*item["source_refs"], *item["counter_source_refs"]]}
            claims, refs = candidate, set()
        refs.update(ref for claim in claims for ref in [*claim["source_refs"], *claim["counter_source_refs"]])
        if refs - allowed:
            raise ValueError("记忆候选引用了冻结来源之外的事实")
        return {"validated": {"sources_checked": True}}

    async def review(state, runtime: Runtime[MemoryRuntime]):
        if state["kind"] != "scope":
            return {"audit": {}}
        runner, metrics = runtime.context.runner, deepcopy(state["metrics"])
        try:
            claims, audit = await review_candidate(runner.hierarchy.models, state["snapshot"]["model"],
                state["validated"], metrics, runner.hierarchy.review_models)
        finally:
            runner._failure_metrics[state["job_id"]] = metrics
        return {"candidate": claims, "audit": audit, "metrics": metrics}

    async def stage(state, runtime: Runtime[MemoryRuntime]):
        from app.domains.memory.document_staging import stage_candidate_documents
        await stage_candidate_documents(runtime.store, runtime.context.runner,
            state["kind"], state["snapshot"], state["candidate"])
        return {}

    async def publish(state, runtime: Runtime[MemoryRuntime]):
        runner, snapshot = runtime.context.runner, _bound_snapshot(state, runtime)
        metrics = runner.metric_delta(state["job_id"], state["metrics"])
        if state["kind"] == "extract":
            outcome = runner.pipeline.publish_extraction(snapshot, state["candidate"], metrics)
        elif state["kind"] == "concept":
            outcome = runner.pipeline.publish_concept(snapshot, state["candidate"], metrics)
        else:
            outcome = runner.hierarchy.publish_scope(snapshot, state["candidate"], state["audit"], metrics)
        return {"outcome": outcome or {"status": "superseded"}}

    async def followup(state, runtime: Runtime[MemoryRuntime]):
        if state["kind"] == "extract":
            await runtime.context.runner.pipeline.followup_extraction(state["snapshot"], state["candidate"], state["outcome"])
        elif state["kind"] == "scope" and state["snapshot"]["kind"] == "course" and state["outcome"].get("status") == "completed":
            # 课程发布已同事务登记总体唤醒；即时继续失败也不回滚已完成的课程。
            try:
                await runtime.context.runner.hierarchy._aggregate_scope(state["snapshot"]["student"], "student")
            except Exception:
                pass
        return {}

    builder = StateGraph(MemoryState, context_schema=MemoryRuntime)
    for name, node in (("freeze", freeze), ("generate", generate), ("validate", validate),
                       ("review", review), ("stage", stage), ("publish", publish), ("followup", followup)):
        # 仅幂等Store预写交给框架退避；模型调用仍受持久任务的5/3次总预算约束。
        retry = RetryPolicy(max_attempts=3, initial_interval=0.25, max_interval=2, jitter=False,
            retry_on=(TimeoutError, ConnectionError, OperationalError, PoolTimeout)) if name == "stage" else None
        builder.add_node(name, node, retry_policy=retry)
    for left, right in zip((START, "freeze", "generate", "validate", "review", "stage", "publish", "followup"),
                           ("freeze", "generate", "validate", "review", "stage", "publish", "followup", END)):
        builder.add_edge(left, right)
    return builder.compile(checkpointer=checkpointer, store=store)


class MemoryGraphRunner:
    def __init__(self, service=None, *, hierarchy=None, checkpointer=None, store=None):
        self.service, self._hierarchy = service, hierarchy
        self._failure_metrics = {}
        self._failure_audits = {}
        self._metric_baselines = {}
        self._failure_accounting = {}
        # 持久组件由装配层显式注入；无saver仅提供隔离领域调用，绝不自动降级存储。
        self.checkpointer = checkpointer
        self.graph = build_memory_graph(checkpointer=self.checkpointer, store=store)

    @property
    def pipeline(self):
        return self.service.business_pipeline

    @property
    def hierarchy(self):
        return self._hierarchy or self.pipeline.hierarchy

    def failure_metrics(self, job_id):
        return self.metric_delta(job_id, self._failure_metrics.get(job_id, {}))

    def metric_delta(self, job_id, metrics):
        """检查点中已成功的模型步骤不能在恢复复核或发布时重复累计用量。"""
        baseline = self._metric_baselines.get(job_id, {})
        return {key: max(0, value - baseline.get(key, 0)) for key, value in metrics.items()}

    def failure_accounting(self, job_id):
        return self._failure_accounting.get(job_id, {})

    async def run(self, kind, snapshot):
        job_id = snapshot["job_id"]
        config = {"configurable": {"thread_id": "memory:" + job_id, "checkpoint_ns": ""}, "recursion_limit": 100}
        current = await self.graph.aget_state(config) if self.checkpointer is not None else None
        same_input = bool(current and current.values and _identity(current.values.get("snapshot", {})) == _identity(snapshot))
        if current and current.next == ("validate",) and any(
                task.name == "validate" and task.error for task in current.tasks):
            # 确定性候选校验失败需重生成；不能反复验证同一个坏候选消耗所有预算。
            same_input = False
        if same_input and not current.next:
            return current.values.get("outcome", {})
        database = self.hierarchy.database if kind == "scope" else self.service.database
        with database() as db:
            job = db.get(MemoryJob, job_id)
            accounted = (job.result_snapshot or {}).get("graph_usage_accounted", {})
        self._metric_baselines[job_id] = accounted.get("metrics", {}) if same_input and accounted.get("input") == _identity(snapshot) else {}
        self._failure_metrics[job_id] = deepcopy(current.values.get("metrics", {})) if same_input else {}
        initial = None if same_input else {"job_id": job_id, "kind": kind, "snapshot": snapshot,
            "candidate": None, "validated": None, "audit": {}, "metrics": {}, "outcome": {}}
        try:
            result = await self.graph.ainvoke(initial, config=config,
                context=MemoryRuntime(self, snapshot["lease_token"]),
                **({"durability": "sync"} if self.checkpointer is not None else {}))
        except Exception:
            checkpoint = await self.graph.aget_state(config) if self.checkpointer is not None else None
            self._failure_accounting[job_id] = {"input": _identity(snapshot),
                "metrics": deepcopy(checkpoint.values.get("metrics", {})) if checkpoint else {}}
            raise
        self._failure_metrics.pop(job_id, None)
        return result.get("outcome", {})
