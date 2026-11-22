"""记忆与压缩验收的只读数据库审计，不读取参考解来指导模拟作答。"""

from collections import Counter
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import psycopg

ROOT = Path(__file__).resolve().parents[2]


def source_digest():
    digest = sha256()
    for path in sorted((ROOT / "backend/app").rglob("*.py")):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def digest_json(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


@contextmanager
def _checkpoint_source(path, serializer):
    """当前态经官方 PG saver 只读查询；显式文件仅用于历史归档审计。"""
    if path is not None:
        source = Path(path).resolve()
        if not source.is_file():
            yield {"backend": "archived_sqlite", "available": False,
                   "path": str(source), "reason": "checkpoint_file_missing"}, None
            return
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as connection:
            tables = connection.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            def rows(session_id):
                return connection.execute(
                    "SELECT checkpoint_ns, checkpoint_id, type, checkpoint FROM checkpoints "
                    "WHERE thread_id=? ORDER BY checkpoint_ns, checkpoint_id", ("learning:" + session_id,))
            yield {"backend": "archived_sqlite", "available": True,
                   "path": str(source), "sqlite_table_count": tables}, rows
        return
    from psycopg.rows import dict_row
    from langgraph.checkpoint.postgres import PostgresSaver
    from app.infrastructure.config import get_settings

    with psycopg.connect(get_settings().postgres_dsn(), autocommit=True,
                        row_factory=dict_row, options="-c search_path=langgraph_checkpoint") as connection:
        connection.execute("SET default_transaction_read_only = on")
        saver = PostgresSaver(connection, serde=serializer)
        def rows(session_id):
            values = list(saver.list({"configurable": {"thread_id": "learning:" + session_id}}))
            values.sort(key=lambda item: (item.config["configurable"].get("checkpoint_ns", ""),
                                          item.config["configurable"]["checkpoint_id"]))
            return [(item.config["configurable"].get("checkpoint_ns", ""),
                     item.config["configurable"]["checkpoint_id"], "native", item.checkpoint) for item in values]
        yield {"backend": "native_postgresql", "schema": "langgraph_checkpoint", "available": True}, rows


def read_native_checkpoints(session_ids: list[str], path: Path | None = None) -> dict:
    """只读实际检查点消息，不把检查点数量当作摘要证据。"""
    from langchain_core.messages import convert_to_messages, convert_to_openai_messages
    from app.application.learning.storage import checkpoint_serde
    from app.agent.context.builder import history_unit
    from app.shared.llm_budget import estimate_request

    result = {"backend": "native_postgresql" if path is None else "archived_sqlite", "available": False,
              "checkpoint_rows_inspected": 0, "summaries": []}
    serializer = checkpoint_serde()
    evidence, latest_root = {}, {}

    def summary(message):
        if message.additional_kwargs.get("lc_source") != "summarization" or not isinstance(message.content, str):
            return None
        try:
            value = json.loads(message.content.split("\n", 1)[1])
        except (IndexError, json.JSONDecodeError):
            return None
        refs = value.get("source_refs") if isinstance(value, dict) else None
        return value if isinstance(refs, list) and refs and all(isinstance(item, str) for item in refs) else None

    def identity(message):
        return message.id or digest_json(convert_to_openai_messages([message])[0])

    try:
        with _checkpoint_source(path, serializer) as (metadata, checkpoint_rows):
            result.update(metadata)
            if not result["available"]:
                return result
            for session_id in dict.fromkeys(session_ids):
                previous = {}
                rows = checkpoint_rows(session_id)
                for namespace, checkpoint_id, kind, payload in rows:
                    result["checkpoint_rows_inspected"] += 1
                    if kind not in {"json", "msgpack", "native"}:
                        result["unsupported_checkpoint_types"] = result.get("unsupported_checkpoint_types", 0) + 1
                        continue
                    try:
                        checkpoint = payload if kind == "native" else serializer.loads_typed((kind, payload))
                        messages = convert_to_messages(checkpoint.get("channel_values", {}).get("messages", []))
                    except Exception:
                        result["decode_failures"] = result.get("decode_failures", 0) + 1
                        continue
                    prior = previous.get(namespace)
                    prior_summaries = {digest_json(value) for message in prior or [] if (value := summary(message))}
                    retained = {identity(message) for message in messages}
                    removed = [(index, message) for index, message in enumerate(prior or []) if identity(message) not in retained]
                    for message in messages:
                        value = summary(message)
                        if not value or digest_json(value) in prior_summaries:
                            continue
                        refs = value["source_refs"]
                        expected = [ref for index, old in removed for ref in (
                            old.additional_kwargs.get("tutor_source_refs") or
                            [history_unit("message", index, convert_to_openai_messages([old])[0])["source_ref"]]
                        )]
                        valid = (bool(prior and expected) and set(refs) == set(expected)
                                 and len(refs) == len(set(refs))
                                 and message.additional_kwargs.get("tutor_source_refs") == refs)
                        before = estimate_request(convert_to_openai_messages([old for _, old in removed])).tokens if removed else None
                        after = estimate_request(convert_to_openai_messages([message])).tokens
                        record = {
                            "id": "native:" + digest_json(value), "session_id": session_id,
                            "checkpoint_id": checkpoint_id, "checkpoint_ns": namespace,
                            "covered_count": len(refs), "source_ref_count": len(refs),
                            "source_coverage_valid": valid,
                            "coverage_evidence": "removed_messages_in_prior_native_snapshot" if valid else "independent_coverage_unavailable",
                            "raw_sources_strategy": "formal_messages_and_native_checkpoints",
                            "summary_sha256": digest_json(value), "source_fingerprint": digest_json(refs),
                            "source_message_tokens": before, "summary_message_tokens": after,
                            "compression_reduced": before is not None and before > after,
                            "token_measurement": "local_message_estimator_without_tools",
                        }
                        key = (session_id, record["summary_sha256"])
                        old = evidence.get(key)
                        if old is None or (valid, record["compression_reduced"]) > (old["source_coverage_valid"], old["compression_reduced"]):
                            evidence[key] = record
                    previous[namespace] = messages
                    if not namespace:
                        latest_root[session_id] = messages
    except (sqlite3.Error, psycopg.Error) as exc:
        return {**result, "available": False, "reason": "checkpoint_read_failed", "error_type": type(exc).__name__}
    # 子图与父图可能持有同一条摘要，只统计最新根状态实际保留的摘要一次。
    for session_id, messages in latest_root.items():
        for message in messages:
            if value := summary(message):
                record = evidence.get((session_id, digest_json(value)))
                if record and record not in result["summaries"]:
                    result["summaries"].append(record)
    if path is not None:
        result["file_bytes"] = Path(path).stat().st_size
        wal = Path(str(path) + "-wal")
        result["wal_bytes"] = wal.stat().st_size if wal.is_file() else 0
    return result


def read_audit(student_id: str, session_ids: list[str]) -> dict:
    """仅SELECT验收学生的元数据和公开事实指纹；不导出题库解答或内部契约。"""
    from sqlalchemy import select

    from app.infrastructure.config import get_settings
    from app.infrastructure.persistence.database import db_session
    from app.infrastructure.persistence.execution_models import AgentRun
    from diagnostic_events import read_diagnostic_events
    from app.domains.sessions.models import LearningSession, Message
    from app.domains.students.models import StudentState
    from app.domains.memory.models import MemoryFact, MemoryJob
    from app.domains.memory.scopes.models import ScopeMemory, SessionMemoryExtraction
    from app.domains.memory.scopes.identity import scope_key
    from app.domains.sessions.models import SessionGoalState

    with db_session() as db:
        sessions = list(db.scalars(select(LearningSession).where(
            LearningSession.id.in_(session_ids), LearningSession.student_id == student_id)))
        owned = [session.id for session in sessions]
        messages = list(db.scalars(select(Message).where(Message.session_id.in_(owned))))
        facts = list(db.scalars(select(MemoryFact).where(MemoryFact.session_id.in_(owned))))
        jobs = list(db.scalars(select(MemoryJob).where(MemoryJob.session_id.in_(owned))))
        runs = list(db.scalars(select(AgentRun).where(AgentRun.session_id.in_(owned)).order_by(AgentRun.created_at)))
        events = read_diagnostic_events(owned)
        experiences = list(db.scalars(select(SessionMemoryExtraction).where(SessionMemoryExtraction.session_id.in_(owned))))
        native = read_native_checkpoints(owned)
        states = list(db.scalars(select(StudentState).where(StudentState.student_id == student_id)))
        student = db.get(ScopeMemory, scope_key(student_id,"student"))
        state_rows = [{"id": state.id, "version": state.version, "mastery": state.mastery,
                       "evidence_ids": state.evidence_ids} for state in states]
        goals = [db.get(SessionGoalState, sid) for sid in owned]
        selected_events = [{"id": event.id, "session_id": event.session_id, "turn_id": event.turn_id,
                            "event": event.event, "data": event.data} for event in events
                           if event.event in {"context_budget", "model_request_completed", "memory_context_loaded", "error", "agent_failed"}]
        return {
            "database": get_settings().database_name,
            "models": sorted({run.model for run in runs}),
            "student_state_count": len(states),
            "student_state_sha256": digest_json(sorted(state_rows, key=lambda item: item["id"])),
            "student_memory": ({"version": student.version, "characters": len(student.content),
                                "content_sha256": sha256(student.content.encode()).hexdigest(),
                                "source_ref_count": len(student.source_refs)} if student else None),
            "messages": [{"id": message.id, "session_id": message.session_id, "role": message.role,
                          "characters": len(message.content), "content_sha256": sha256(message.content.encode()).hexdigest()}
                         for message in messages],
            "message_counts": dict(Counter(message.session_id for message in messages)),
            "fact_counts": dict(Counter(fact.session_id for fact in facts)),
            "fact_source_unique": len({(fact.session_id, fact.source_ref) for fact in facts}) == len(facts),
            "experiences": [{"session_id": item.session_id, "version": item.experience_version,
                             "status": item.experience_status, "covered_seq": item.experience_covered_seq,
                             "characters": len(item.content), "source_ref_count": len(item.experience_source_refs)} for item in experiences],
            "experience_counts": dict(Counter(item.session_id for item in experiences)),
            "jobs": [{"id": job.id, "session_id": job.session_id, "target_seq": job.target_seq,
                      "close_version": job.close_version, "final": job.final, "status": job.status,
                      "attempts": job.attempts, "reason": job.reason, "error": job.error,
                      "metrics": job.metrics} for job in jobs],
            "checkpoints": native["summaries"],
            "checkpoint_storage": {key: value for key, value in native.items() if key != "summaries"},
            "runs": [{"id": run.id, "session_id": run.session_id, "agent": run.agent, "model": run.model,
                      "status": run.status, "duration_ms": run.duration_ms, "prompt_tokens": run.prompt_tokens,
                      "completion_tokens": run.completion_tokens, "tool_calls": len(run.tool_calls),
                      "error": run.error} for run in runs],
            "events": selected_events,
            "goal_states": [{"session_id": state.session_id, "root_goal_id": state.root_goal_id,
                             "active_goal_id": state.active_goal_id, "version": state.version,
                             "node_count": len(state.nodes), "return_depth": len(state.return_stack)}
                            for state in goals if state is not None],
            "guidance_integrity": [{
                "session_id": session.id, "workspace_id": workspace.get("id"),
                "problem_id": workspace.get("problem", {}).get("id"),
                "problem_stem_sha256": sha256(workspace.get("problem", {}).get("stem", "").encode()).hexdigest(),
                "pending_question_id": workspace.get("pending_question_id"),
                "pending_question_present": bool(workspace.get("pending_question")),
                "assisted": workspace.get("assisted"), "help_level": workspace.get("help_level"),
                "evidence_count": len(workspace.get("evidence", [])),
                "evidence_raw_input_sha256": [sha256(item.get("raw_input", "").encode()).hexdigest()
                                              for item in workspace.get("evidence", [])],
                "support_history_count": len(workspace.get("support_history", [])),
                "dialogue_count": len(workspace.get("dialogue", [])),
            } for session in sessions if (workspace := (session.pending_context or {}).get("guided_teaching"))],
        }


def verify_stress_trace(report: dict, audit: dict, problem: str) -> dict:
    """分别校验聊天展示契约和原始提交证据，首轮消息按约定保存完整题干。"""
    session_id = report["sessions"]["stress"]
    turns = [turn for turn in report["turns"] if turn["session_id"] == session_id]
    integrity = next(item for item in audit["guidance_integrity"] if item["session_id"] == session_id)
    assert integrity["problem_stem_sha256"] == sha256(problem.encode()).hexdigest(), "原题内容变化"
    assert integrity["pending_question_present"] and integrity["pending_question_id"], "当前待答题缺失"
    assert integrity["evidence_count"] == len(turns), "提交证据数量不一致"
    messages = {item["content_sha256"] for item in audit["messages"]
                if item["session_id"] == session_id and item["role"] == "user"}
    expected_messages = [problem, *[turn["student_text"] for turn in turns[1:]]]
    assert all(sha256(value.encode()).hexdigest() in messages for value in expected_messages), "原始聊天消息缺失或被改写"
    raw_inputs = set(integrity["evidence_raw_input_sha256"])
    assert all(sha256(turn["student_text"].encode()).hexdigest() in raw_inputs for turn in turns), "提交原文证据缺失或被改写"
    budgets = [event["data"] for event in audit["events"]
               if event["event"] == "context_budget" and event["session_id"] == session_id]
    compressed = [item for item in budgets if "compressed" in item.get("status", "")]
    assert compressed, "没有真实压缩事件"
    checkpoints = [item for item in audit["checkpoints"] if item["session_id"] == session_id]
    assert checkpoints and all(item["source_coverage_valid"] for item in checkpoints), "检查点来源覆盖不完整"
    assert all(item.get("compression_reduced") for item in checkpoints), "持久摘要缺少消息估算下降证据"
    sent = [event["data"]["context"] for event in audit["events"]
            if event["event"] == "model_request_completed" and event["session_id"] == session_id
            and event["data"].get("context", {}).get("input_budget") is not None]
    assert sent and all(item["estimated_input_tokens"] <= item["input_budget"] for item in sent), "缺少真实模型请求预算证据或存在超预算请求"
    return {"rounds": len(turns), "compressed_events": compressed, "checkpoint_count": len(checkpoints),
            "same_problem_id": integrity["problem_id"], "original_messages_preserved": True,
            "all_submission_raw_inputs_preserved": True, "guidance_evidence_preserved": True,
            "compression_evidence": "native_saved_message_transition_and_observed_event",
            "provider_full_payload_capture": False}


def usage_totals(audit: dict) -> dict:
    runs, jobs = audit.get("runs", []), audit.get("jobs", [])
    requests = [event["data"] for event in audit.get("events", []) if event["event"] == "model_request_completed"]
    budgets = [event["data"] for event in audit.get("events", []) if event["event"] == "context_budget"]
    return {
        "agent_runs": len(runs), "failed_agent_runs": sum(run["status"] != "completed" for run in runs),
        "agent_prompt_tokens_including_compaction": sum(run["prompt_tokens"] for run in runs),
        "agent_completion_tokens_including_compaction": sum(run["completion_tokens"] for run in runs),
        "memory_model_calls": sum(job.get("metrics", {}).get("model_calls", 0) for job in jobs),
        "memory_prompt_tokens": sum(job.get("metrics", {}).get("prompt_tokens", 0) for job in jobs),
        "memory_completion_tokens": sum(job.get("metrics", {}).get("completion_tokens", 0) for job in jobs),
        "memory_job_attempts": sum(job["attempts"] for job in jobs),
        "memory_failed_or_retry_jobs": sum(job["status"] in {"failed", "retry"} for job in jobs),
        "observed_teaching_model_http_attempts": sum(item.get("request_attempts", 1) for item in requests),
        "observed_teaching_model_http_retries": sum(max(0, item.get("request_attempts", 1) - 1) for item in requests),
        "context_budget_events": len(budgets),
        "compressed_events": sum("compressed" in item.get("status", "") for item in budgets),
        "rejected_budget_events": sum(item.get("status") == "rejected" or item.get("status", "").startswith("blocked") for item in budgets),
        "memory_usage_unavailable_calls": sum(job.get("metrics", {}).get("usage_unavailable_calls", 0) for job in jobs),
        "cost_scope": "AgentRun已含压缩调用用量；MemoryJob单独计入。失败记忆任务未返回usage时成本不可完整恢复，报告不声称总账无遗漏。",
    }
