"""N2／N4 真实模型验收：实际应用生命周期、唯一业务入口及公开 SSE。"""

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))


def sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def save(report, target):
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def protected_snapshot(student_id):
    """只读记录其他学生正式事实的摘要，防止验收误改其他学习范围。"""
    from sqlalchemy import select, and_, or_
    from app.infrastructure.persistence.database import db_session, Base
    from app.domains.sessions.models import LearningSession
    from app.domains.memory.scopes.models import ScopeMemory
    result = {}
    with db_session() as database:
        sessions = select(LearningSession.id).where(LearningSession.student_id != student_id)
        for table in Base.metadata.sorted_tables:
            if table.name == "memory_jobs":
                owned_sessions = select(LearningSession.id).where(LearningSession.student_id == student_id)
                owned_scopes = select(ScopeMemory.scope_key).where(ScopeMemory.student_id == student_id)
                statement = select(table).where(~or_(and_(table.c.session_id.is_not(None), table.c.session_id.in_(owned_sessions)),
                    and_(table.c.scope_key.is_not(None), table.c.scope_key.in_(owned_scopes))))
            elif "student_id" in table.c:
                statement = select(table).where(table.c.student_id != student_id)
            elif "session_id" in table.c:
                statement = select(table).where(table.c.session_id.in_(sessions))
            else:
                continue
            rows = list(database.execute(statement.order_by(*table.primary_key.columns)).mappings())
            result[table.name] = {"rows": len(rows), "sha256": sha([dict(row) for row in rows])}
    return result


def preflight(report):
    """lifespan会恢复全局遗留状态；存在其他学生待恢复状态时停止验收。"""
    from sqlalchemy import select
    from app.infrastructure.persistence.database import db_session
    from app.domains.sessions.models import LearningSession
    from app.infrastructure.persistence.execution_models import AgentRun
    from app.domains.memory.models import MemoryJob
    from app.domains.memory.scopes.models import ScopeMemory
    from app.domains.memory.concepts.observations import RULE_VERSION
    from app.domains.memory.scopes.promotion import AGGREGATION_RULE
    from app.infrastructure.config import get_settings
    # 另一个正常服务的全局worker会抢占本学生任务，审计将无法归属实际模型调用。
    with socket.socket() as probe:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind(("127.0.0.1", get_settings().api_port))
        except OSError:
            raise RuntimeError("Tutor服务端口仍被占用，请先停止其全局记忆worker后在维护窗口验收") from None
    with db_session() as database:
        own_sessions = set(database.scalars(select(LearningSession.id).where(LearningSession.student_id == report["student_id"])))
        own_scopes = set(database.scalars(select(ScopeMemory.scope_key).where(ScopeMemory.student_id == report["student_id"])))
        assert set(report["sessions"].values()).issubset(own_sessions), "报告中的会话归属不匹配，拒绝执行"
        assert not database.scalar(select(LearningSession.id).where(LearningSession.student_id != report["student_id"],
            LearningSession.active_turn_id.is_not(None)).limit(1)), "其他学生正在学习，请停止服务写入后再验收"
        assert not database.scalar(select(AgentRun.id).where(AgentRun.status == "running",
            (AgentRun.session_id.is_(None) | AgentRun.session_id.not_in(own_sessions))).limit(1)), "其他学生存在运行中Agent，拒绝全局恢复"
        for job in database.scalars(select(MemoryJob).where(MemoryJob.status.in_(["pending", "retry", "running"]))):
            if job.session_id in own_sessions or job.scope_key in own_scopes:
                continue
            stale = job.extractor_version != RULE_VERSION if job.job_kind == "session_extract" else job.rule_version not in {RULE_VERSION, AGGREGATION_RULE}
            assert job.status != "running" and not stale, "其他学生有待恢复记忆任务，请在维护窗口处理后再验收"


def select_topic(course_id=None):
    from sqlalchemy import select
    from app.infrastructure.persistence.database import db_session
    from app.domains.courses.models import Course, Concept
    with db_session() as database:
        available = list(database.scalars(select(Course)))
        priorities = [course_id] if course_id else ["junior_algebra", "junior_math_7a", "math", "mechanics"]
        selected = next((row for key in priorities for row in available if row.id == key), None)
        if selected is None:
            raise ValueError("未找到已有验收课程，请用--course-id选择现有课程")
        topics = list(database.scalars(select(Concept).where(Concept.course_id == selected.id, Concept.topic_type != "category")))
        names = ("正数和负数", "有理数", "加乘原理", "分类加法", "分步乘法")
        topic = next((row for name in names for row in topics if name in row.name), None)
        topic = topic or next((row for row in topics if row.summary), None)
        if topic is None:
            raise ValueError("课程没有可教学知识点")
        return {"course_id": selected.id, "course_title": selected.title, "concept_id": topic.id, "concept_name": topic.name}


def run_rows(session_id):
    from sqlalchemy import select
    from app.infrastructure.persistence.database import db_session
    from app.infrastructure.persistence.execution_models import AgentRun
    with db_session() as database:
        return [{"id": row.id, "agent": row.agent, "status": row.status,
                 "prompt_tokens": row.prompt_tokens, "completion_tokens": row.completion_tokens,
                 "tool_calls": row.tool_calls, "private_diagnostics": row.private_diagnostics}
                for row in database.scalars(select(AgentRun).where(AgentRun.session_id == session_id))]


async def actual_sse(app, session_id, body):
    """直接执行实际 ASGI HTTP 入口，逐个接收 response.body，避免测试客户端缓冲。"""
    path = f"/api/sessions/{session_id}/messages"
    events, status = [], None
    started, sent = time.perf_counter(), False
    pending = ""

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": json.dumps(body, ensure_ascii=False).encode(), "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        nonlocal status, pending
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body":
            pending += message.get("body", b"").decode()
            while "\n\n" in pending:
                block, pending = pending.split("\n\n", 1)
                fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
                if "event" in fields:
                    events.append({"event": fields["event"], "data": json.loads(fields.get("data", "{}")),
                                   "at_ms": round((time.perf_counter()-started)*1000, 3)})

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"stream=true",
             "root_path": "", "headers": [(b"content-type", b"application/json"), (b"host", b"evaluation.local")],
             "client": ("127.0.0.1", 12345), "server": ("evaluation.local", 80)}
    await app(scope, receive, send)
    assert status == 200, f"实际SSE HTTP状态异常：{status}"
    assert not any(item["event"] == "error" for item in events), "实际SSE返回业务错误"
    deltas = [item for item in events if item["event"] == "text_delta"]
    completed = [item for item in events if item["event"] == "agent_completed"]
    assert deltas and completed and deltas[0]["at_ms"] < completed[-1]["at_ms"], "正文没有在模型完成前公开"
    assert any(item["event"] == "done" for item in events), "SSE缺少完成事件"
    return {"http_status": status, "transport": "actual_asgi_http_response_body_chunks",
            "events": dict(Counter(item["event"] for item in events)), "delta_count": len(deltas),
            "first_delta_ms": deltas[0]["at_ms"], "agent_completed_ms": completed[-1]["at_ms"],
            "public_fields": sorted({item["data"]["field"] for item in deltas}),
            "public_text_sha256": sha("".join(item["data"]["delta"] for item in deltas))}


class Acceptance:
    """仅观察真实调用，业务执行全部经过正式服务；报告同时支持恢复原请求。"""

    def __init__(self, arguments, target, report):
        self.arguments, self.target, self.report = arguments, target, report
        self.stage, self.events = "startup", []

    def save(self):
        save(self.report, self.target)

    async def check(self, name, action):
        previous = next((item for item in reversed(self.report["checks"]) if item["name"] == name), None)
        if self.arguments.resume and previous and previous["passed"]:
            return
        self.stage = name
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self.arguments.stage_timeout):
                item = {"name": name, "passed": True, "details": await action()}
        except Exception as exc:
            item = {"name": name, "passed": False, "error_type": type(exc).__name__, "error": str(exc)[:400]}
        item["seconds"] = round(time.perf_counter() - started, 3)
        self.report["checks"].append(item)
        self.save()
        print(json.dumps(item, ensure_ascii=False), flush=True)

    async def emit(self, name, value):
        self.events.append((name, value))
        if name == "model_request_completed":
            rounds = self.report.setdefault("model_rounds", {})
            rounds[value["run_id"]] = rounds.get(value["run_id"], 0) + 1
            self.save()
        if name == "batch_item_started":
            indices = self.report.setdefault("batch_task_indices", [])
            if value["index"] not in indices:
                indices.append(value["index"])
            self.save()
        if name in {"agent_started", "agent_completed", "agent_failed", "batch_item_started", "batch_item_completed"}:
            print(json.dumps({"stage": self.stage, "event": name, "agent": value.get("agent"), "index": value.get("index")}, ensure_ascii=False), flush=True)

    async def on_request(self, request):
        if len(self.report["requests"]) >= self.arguments.max_requests:
            raise RuntimeError("隔离验收模型请求预算已到，保留报告后可显式恢复")
        try:
            body = json.loads(request.content)
        except (ValueError, TypeError):
            body = {}
        request.extensions["acceptance_index"] = len(self.report["requests"])
        self.report["requests"].append({"stage": self.stage, "path": request.url.path, "model": body.get("model"), "stream": body.get("stream", False),
            "functions": [item["function"]["name"] for item in body.get("tools", [])], "response_format": body.get("response_format")})
        self.save()

    async def on_response(self, response):
        self.report["requests"][response.request.extensions["acceptance_index"]]["http_status"] = response.status_code
        self.save()

    async def observe_structured(self, *args, **kwargs):
        result = await self.original_structured(*args, **kwargs)
        self.report.setdefault("structured_calls", []).append({"stage": self.stage, "schema": args[2].__name__,
            "parsed": result.get("parsed") is not None, "usage_complete": result.get("usage_complete"), "usage": result["raw"].usage_metadata})
        self.save()
        return result

    def session(self, name, goal):
        if name not in self.report["sessions"]:
            row = self.services["sessions"].create(goal, [self.report["concept_id"]], self.report["course_id"], self.report["student_id"])
            self.report["sessions"][name] = row["id"]
            self.save()
        return self.report["sessions"][name]

    async def execute(self, session, event):
        return await self.services["learning"].execute(session, event, str(uuid4()), self.emit)

    async def delegation(self):
        topic = self.report["concept_name"]
        session = self.session("knowledge", f"只学习{topic}的课本定义，本轮不出题")
        await self.execute(session, {"kind": "message", "request_id": "native-knowledge-" + self.report["student_id"],
            "intent": "teach", "concept_ids": [self.report["concept_id"]],
            "content": f"请先实际读取{topic}的课本定义和教学卡片，再委派知识点Agent用教材中的方法简短讲解；读取子Agent结果后由主Agent简短衔接，本轮不安排练习。"})
        runs = run_rows(session)
        roots = [row for row in runs if row["agent"] == "supervisor" and row["status"] == "completed"]
        assert roots and any(row["agent"] == "knowledge" and row["status"] == "completed" for row in runs), "没有实际完成知识点委派"
        root = roots[-1]
        calls = root["tool_calls"]
        assert any(call["name"] == "get_concept_details" and call["status"] == "completed" for call in calls), "未执行真实课本工具"
        delegated = next(call for call in calls if call["name"] == "call_knowledge_agent" and call["status"] == "completed")
        assert delegated.get("child_run_ids"), "委派缺少子运行回执"
        rounds = self.report.get("model_rounds", {}).get(root["id"], 0)
        assert rounds >= 2, "子工具返回后原主模型没有继续执行"
        return {"session_id": session, "supervisor_run_id": root["id"], "child_run_ids": delegated["child_run_ids"],
            "supervisor_model_rounds": rounds, "executed_tools": [{"name": call["name"], "status": call["status"]} for call in calls]}

    async def batch(self):
        from sqlalchemy import select, func
        from app.domains.sessions.models import LearningSession, Message
        from app.domains.assessment.models import Attempt, BatchSubmission
        from app.infrastructure.persistence.database import db_session

        topic = self.report["concept_name"]
        session = self.session("batch", f"完成两道{topic}基础单空填空题，然后结束本轮练习，不追加题目")
        # 稳定请求身份与正式回执接管恢复，不重新生成已发布题或替换等待点。
        await self.execute(session, {"kind": "message", "request_id": "native-generate-" + self.report["student_id"],
            "intent": "practice", "generate": True, "exercise_count": 2, "cognitive_type": "understanding", "concept_ids": [self.report["concept_id"]],
            "content": f"请实际并行生成两道{topic}的基础单空填空题，数字简单，每题只有一个空和唯一数值答案。两题组成一组供我一次提交，本轮完成这两题后结束，不再追加新题。"})
        with db_session() as database:
            rows = list(database.scalars(select(Message).where(Message.session_id == session)))
            request = next(((row.metadata_json or {}).get("_native_request", {}).get("event") for row in rows
                if (row.metadata_json or {}).get("_native_request", {}).get("event", {}).get("kind") == "batch_answer"), None)
            if request is None:
                batch = json.loads(json.dumps(database.get(LearningSession, session).pending_context.get("exercise_batch")))
                assert batch and len(batch["items"]) == 2, "未正式发布两道题"
                answers = []
                for item in batch["items"]:
                    question = item["contract"]
                    assert question["question_type"] == "fill", "本次工程验收限定单空填空题"
                    answer = question["answer"]
                    answers.append({"question_id": question["id"], "answer": str(answer[0]) if isinstance(answer, list) else str(answer)})
                wait = self.services["sessions"].get(session)["pending_interaction"]
                request = {"kind": "batch_answer", "request_id": "native-grade-" + self.report["student_id"],
                    "wait_id": wait["wait_id"], "batch_id": batch["id"], "answers": answers}
        assert set(self.report.get("batch_task_indices", [])) == {1, 2}, "两条真实Send分支未执行"
        before = len(self.report.get("structured_calls", []))
        result = await self.execute(session, request)
        grading = [item for item in self.report.get("structured_calls", [])[before:] if item["schema"] == "BatchDiagnosis"]
        if not result.get("replayed"):
            assert len(grading) == 1 and grading[0]["parsed"], "整组评分未在一次真实结构化调用完成"
        with db_session() as database:
            attempts = database.scalar(select(func.count()).select_from(Attempt).where(Attempt.session_id == session))
            submissions = database.scalar(select(func.count()).select_from(BatchSubmission).where(BatchSubmission.session_id == session))
        assert attempts == 2 and submissions == 1, "正式评分产物数量不正确"
        calls = len(self.report["requests"])
        replay = await self.execute(session, request)
        assert replay.get("replayed") and len(self.report["requests"]) == calls, "重复请求调用了模型"
        return {"session_id": session, "batch_id": request["batch_id"], "question_ids": [item["question_id"] for item in request["answers"]],
            "answer_source": "frozen validated references; engineering acceptance, not actual student mastery", "assessment_model_calls": len(grading),
            "formal_attempts": attempts, "formal_submissions": submissions, "replay_without_model": True}

    async def sse(self):
        session = self.session("sse", "简短了解如何逐步练习，本轮不出题")
        # 重新验证传输需要新的只读交流，正式回执回放不承担重现Token流的责任。
        self.report["sse_attempt"] = self.report.get("sse_attempt", 0) + 1
        self.save()
        return {"session_id": session, **await actual_sse(self.app, session, {"request_id": f"native-sse-{self.report['student_id']}-{self.report['sse_attempt']}",
            "content": "请用三句完整中文说明我可以如何通过逐步思考来学习数学，只作简短交流，本轮不出题。"})}

    async def graph(self):
        retrieval = self.services["retrieval"]
        selected = await retrieval.graph_retrieve(self.report["concept_name"], course_id=self.report["course_id"], seed_concept_ids=[self.report["concept_id"]], top_k=3)

        def receipt(value):
            return {"channels_used": value["channels_used"], "degraded": value["degraded"], "reason": value["reason"],
                "nodes": len(value["graph"]["nodes"]), "edges": len(value["graph"]["edges"]), "items": len(value["items"]),
                "citation_ids": [item["id"] for item in value["citations"]]}

        seeds = await retrieval.graph.query("MATCH (a:StemTutorConcept)-[r]-(b:StemTutorConcept) WHERE a.course_id=b.course_id RETURN a.course_id AS course_id,a.id AS id,a.name AS name LIMIT 1")
        assert seeds, "没有已构图的知识点，不能声称真实图增强生效"
        seed = seeds[0]
        indexed = await retrieval.graph_retrieve(seed["name"], course_id=seed["course_id"], seed_concept_ids=[seed["id"]], top_k=3)
        assert indexed["graph"]["edges"] and indexed["items"] and not indexed["degraded"], "真实图检索未产生有效结果"
        return {"selected_course": receipt(selected), "existing_indexed_seed": {"seed": seed, **receipt(indexed)}}

    async def memory(self):
        from sqlalchemy import select, or_
        from app.infrastructure.persistence.database import db_session
        from app.infrastructure.persistence.defaults import now
        from app.domains.sessions.models import LearningSession
        from app.domains.memory.models import MemoryJob
        from app.domains.memory.scopes.models import ScopeMemory

        memory = self.services["memory"]
        session = self.session("sse", "简短了解如何逐步练习，本轮不出题")
        await self.execute(session, {"kind": "message", "request_id": "native-preference-" + self.report["student_id"], "intent": "auto",
            "content": "这是隔离验收学生的持续偏好：今后所有课程都请先给一句提示，再等我自己思考；数学课程请先说明符号和已知条件，再给计算步骤。本次作答来自系统工程验收，不能推断实际学生掌握程度。现在只确认收到，本轮不出题。"})
        # 讲过只作为exposure事实；不能要求缺少概念归因的整题评分生成概念记忆。
        captured = [memory.capture(identifier, reason="explicit_refresh", force=True)
                    for identifier in (self.report["sessions"]["knowledge"], self.report["sessions"]["batch"], session)]
        # 正式队列的可领取时间采用数据库时间；等到首轮到期后再按job_id推进。
        await asyncio.sleep(1)
        visited = set()
        for _ in range(12):
            with db_session() as database:
                sessions = select(LearningSession.id).where(LearningSession.student_id == self.report["student_id"])
                scopes = select(ScopeMemory.scope_key).where(ScopeMemory.student_id == self.report["student_id"])
                job = database.scalar(select(MemoryJob).where(or_(MemoryJob.session_id.in_(sessions), MemoryJob.scope_key.in_(scopes)),
                    MemoryJob.status.in_(["pending", "retry"]), MemoryJob.available_at <= now(), MemoryJob.id.not_in(visited)).order_by(MemoryJob.created_at))
                if job is None:
                    break
                identifier = job.id
                visited.add(identifier)
            await memory.worker.run_once(job_id=identifier)
        with db_session() as database:
            levels = [{"kind": row.scope_kind, "concept_id": row.concept_id, "version": row.version, "claim_count": len(row.claims),
                "source_refs": row.source_refs, "content_sha256": sha(row.content)} for row in database.scalars(select(ScopeMemory)
                .where(ScopeMemory.student_id == self.report["student_id"])) if row.version]
            jobs = [{"id": row.id, "kind": row.job_kind, "status": row.status, "error": row.error, "metrics": row.metrics}
                for row in database.scalars(select(MemoryJob).where(or_(MemoryJob.session_id.in_(sessions), MemoryJob.scope_key.in_(scopes))))]
        schemas = {item["schema"] for item in self.report.get("structured_calls", []) if item["stage"] == self.stage and item["parsed"]}
        self.report["memory_evidence"] = {"capture": captured, "jobs": jobs, "levels": levels, "schemas": sorted(schemas)}
        self.save()
        assert {"Extraction", "AggregatedClaims", "PromotionCandidate", "PromotionReview"}.issubset(schemas), "三处标准记忆模型及晋升复核尚未实际完成"
        assert {"concept", "course", "student"}.issubset({row["kind"] for row in levels}), "三层记忆未全部持久化"
        assert jobs and all(row["status"] in {"completed", "superseded"} for row in jobs), "定向记忆任务尚未全部完成"
        return {"capture": captured, "jobs": jobs, "levels": levels, "schemas": sorted(schemas),
            "input_scope": "explicit engineering self-report and formal facts; no invented mastery"}


async def main(arguments):
    target = Path(arguments.report).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if arguments.resume:
        report = json.loads(target.read_text(encoding="utf-8"))
        if report.get("evaluation_version") != 1 or not report["student_id"].startswith("evaluation_"):
            raise ValueError("验收报告格式不匹配，拒绝猜测旧报告或真实学生身份")
    else:
        if target.exists():
            target.with_name(f"{target.stem}_previous_{time.time_ns()}.json").write_bytes(target.read_bytes())
        student = "evaluation_" + uuid4().hex[:12]
        report = {"evaluation_version": 1, "started_at": datetime.now(timezone.utc).isoformat(), "student_id": student,
            "scope": "N2/N4 real native agent acceptance; N1 evidence not recounted", "model_mocked": False,
            "checks": [], "requests": [], "structured_calls": [], "sessions": {}, "checkpoint_backend": "postgresql"}
    from app.application.model_registry import register_models
    from app.main import app
    from app.bootstrap import lifespan
    from app.application.agent import structured
    from app.domains.memory.concepts import observations, service
    from app.domains.memory.scopes import promotion
    register_models()
    preflight(report)
    audit = Acceptance(arguments, target, report)
    audit.original_structured = structured.invoke_structured
    modules = (structured, observations, service, promotion)
    for module in modules:
        module.invoke_structured = audit.observe_structured
    before = protected_snapshot(report["student_id"])
    report.update(select_topic(arguments.course_id) if not arguments.resume else {})
    audit.save()
    try:
        async with lifespan(app):
            audit.app, audit.services = app, app.state.services
            # 只定向运行该隔离学生的已有worker，避免轮询消费其他学生任务。
            await audit.services["memory"].worker.stop()
            report["memory_worker"] = "global polling stopped; run_once(job_id) limited to evaluation student"
            models = audit.services["models"]
            report.update(model=models.settings.llm_model, checkpoint_backend="postgresql",
                          checkpoint_saver=type(audit.services["learning"].checkpointer).__name__)
            models.http.event_hooks["request"].append(audit.on_request)
            models.http.event_hooks["response"].append(audit.on_response)
            for name, action in (("native_supervisor_tools_child_return", audit.delegation), ("native_send_batch_and_atomic_submission", audit.batch),
                                 ("actual_public_token_sse", audit.sse), ("live_graph_rag", audit.graph), ("live_recursive_memory", audit.memory)):
                await audit.check(name, action)
    except Exception as exc:
        report.setdefault("errors", []).append({"type": type(exc).__name__, "message": str(exc)[:400]})
    finally:
        for module in modules:
            module.invoke_structured = audit.original_structured
        after = protected_snapshot(report["student_id"])
        report.setdefault("protection_checks", []).append({"before": before, "after": after, "unchanged": before == after})
        report.update(protected_before=before, protected_after=after, other_student_facts_unchanged=before == after,
            finished_at=datetime.now(timezone.utc).isoformat(), complete=True)
        report["current_acceptance"] = {item["name"]: item["passed"] for item in report["checks"]}
        report["request_attempt_count"] = len(report["requests"])
        report["passed"] = len(report["current_acceptance"]) == 5 and all(report["current_acceptance"].values()) and all(item["unchanged"] for item in report["protection_checks"]) and not report.get("errors")
        audit.save()
    print(json.dumps({"passed": report["passed"], "student_id": report["student_id"], "requests": len(report["requests"]), "report": str(target)}, ensure_ascii=False), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--course-id")
    parser.add_argument("--report", default=str(ROOT / "data/reports/native_remaining/real_model.json"))
    parser.add_argument("--max-requests", type=int, default=64)
    parser.add_argument("--stage-timeout", type=float, default=420)
    parser.add_argument("--resume", action="store_true", help="继续报告中的隔离学生原请求，不重新生成已发布题")
    from app.infrastructure.persistence.async_runtime import selector_loop
    with asyncio.Runner(loop_factory=selector_loop) as event_runner:
        raise SystemExit(event_runner.run(main(parser.parse_args())))


