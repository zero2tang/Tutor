"""用两个普通用户请求复查当前后端的原创出题完整链路。

全部写入使用新建 evaluation 档案；原始阶段输出仅进入开发者诊断文件。
本脚本不计算数学答案，不替换模型输出，不改变生产提示或运行预算。
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
from urllib.parse import quote
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import select

from app.infrastructure.clients.search import SearchClient
from app.infrastructure.persistence.database import db_session
from app.infrastructure.persistence.execution_models import AgentRun
from diagnostic_events import read_diagnostic_events
from types import SimpleNamespace
from app.domains.sessions.models import LearningSession
from app.domains.courses.models import Question
from natural_teaching_audit import capture_all_run_usage

CASES = (
    (
        "hard_fill",
        "请给我出一道原创、比较难的和差倍应用题，做成单空填空题。",
        "fill",
    ),
    (
        "ordinary_choice",
        "请给我出一道原创的和差倍应用题，普通难度，做成单选题。",
        "choice",
    ),
)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def source_snapshot():
    """记录当前磁盘源码指纹；后台进程刷新由本轮执行者负责核实。"""
    digest = hashlib.sha256()
    count = 0
    for path in sorted((ROOT / "backend/app").rglob("*")):
        if path.is_file() and path.suffix in {".py", ".md"}:
            digest.update(path.relative_to(ROOT).as_posix().encode())
            digest.update(path.read_bytes())
            count += 1
    return {"sha256": digest.hexdigest(), "file_count": count}


def stage_output(stage):
    return (stage or {}).get("output") or {}


def read_private_audit(session_id):
    """只读取本次隔离会话的三阶段证据及与其关联的新生成题。"""
    with db_session() as db:
        session = db.get(LearningSession, session_id)
        attempts = [SimpleNamespace(**run.private_diagnostics["generation_attempt"])
            for run in db.scalars(select(AgentRun).where(AgentRun.session_id == session_id))
            if (run.private_diagnostics or {}).get("generation_attempt")]
        runs = list(
            db.scalars(
                select(AgentRun)
                .where(AgentRun.session_id == session_id)
                .order_by(AgentRun.created_at, AgentRun.id)
            )
        )
        events = read_diagnostic_events([session_id])
        audit_ids = [row.id for row in attempts]
        questions = (
            list(
                db.scalars(
                    select(Question).where(
                        Question.generated.is_(True),
                        Question.validation["generation_attempt_id"]
                        .as_string()
                        .in_(audit_ids),
                    )
                )
            )
            if audit_ids
            else []
        )
        return {
            "database_dialect": db.get_bind().dialect.name,
            "session": {
                "id": session.id,
                "student_id": session.student_id,
                "course_id": session.course_id,
                "phase": session.phase,
                "active_turn_id": session.active_turn_id,
                "pending_question_id": session.pending_question_id,
            },
            "attempts": [
                {
                    "audit_id": row.id,
                    "turn_id": row.turn_id,
                    "task_index": row.task_index,
                    "attempt_number": row.attempt_number,
                    "status": row.status,
                    "reason": row.reason,
                    "created_at_utc": row.created_at.isoformat() + "Z",
                    "stages": deepcopy(row.details),
                }
                for row in attempts
            ],
            "agent_runs": [
                {
                    "run_id": row.id,
                    "session_id": row.session_id,
                    "turn_id": row.turn_id,
                    "agent": row.agent,
                    "skill": row.skill,
                    "model": row.model,
                    "status": row.status,
                    "error": row.error,
                    "duration_ms": round(row.duration_ms or 0, 3),
                    "prompt_tokens": row.prompt_tokens,
                    "completion_tokens": row.completion_tokens,
                    "tool_calls": deepcopy(row.tool_calls),
                    "created_at_utc": row.created_at.isoformat() + "Z",
                }
                for row in runs
            ],
            "diagnostic_events": [
                {
                    "event_id": row.id,
                    "turn_id": row.turn_id,
                    "seq": row.seq,
                    "event": row.event,
                    "data": deepcopy(row.data),
                }
                for row in events
            ],
            "saved_questions": [
                {
                    "id": row.id,
                    "course_id": row.course_id,
                    "stem": row.stem,
                    "options": deepcopy(row.options),
                    "question_type": row.question_type,
                    "cognitive_type": row.cognitive_type,
                    "concept_ids": deepcopy(row.concept_ids),
                    "answer": deepcopy(row.answer),
                    "explanation": row.explanation,
                    "generated": row.generated,
                    "source_ref": row.source_ref,
                    "validation": deepcopy(row.validation),
                }
                for row in questions
            ],
        }


def public_question_ids(snapshot):
    ids = set()
    question = snapshot.get("pending_question")
    if question and question.get("id"):
        ids.add(question["id"])
    for question in (snapshot.get("pending_batch") or {}).get("questions", []):
        if question.get("id"):
            ids.add(question["id"])
    return ids


def developer_transcript(report):
    lines = [
        "开发者诊断文件：包含未发布草稿、私有答案及复核结果，不自动发送学生。",
        "本次未注入数学答案、计算器、求解器或额外教学约束。",
        "数学正确性需要事后独立核对；模型复核通过不等于程序证明。",
        "",
    ]
    for case in report["cases"]:
        lines += [
            "=" * 72,
            f"案例：{case['case_id']}",
            f"用户原话：{case['user_message']}",
            f"技术执行：{case['execution_status']}；生成结果：{case['generation_status']}",
            f"会话：{case.get('session_id')}；请求：{case['request_body']['request_id']}",
            "",
            "教师正式原话：",
        ]
        for message in case.get("teacher_messages", []):
            lines += [
                f"[{message.get('agent') or 'assistant'}]",
                message["content"],
                "",
            ]
        for question in case.get("private_audit", {}).get("saved_questions", []):
            lines += ["最终保存题目：", question["stem"]]
            if question["options"]:
                lines.append(
                    json.dumps(question["options"], ensure_ascii=False, indent=2)
                )
            lines += [
                "最终答案来源：accepted review.answer → Question.answer",
                json.dumps(question["answer"], ensure_ascii=False),
                "最终解析：",
                question["explanation"],
                "",
            ]
        for attempt in case.get("private_audit", {}).get("attempts", []):
            lines += [
                f"私有调试尝试 {attempt['audit_id']}：{attempt['status']}",
                "拒绝或接受理由：" + attempt["reason"],
                json.dumps(attempt["stages"], ensure_ascii=False, indent=2),
                "",
            ]
        for error in case.get("errors", []):
            lines.append("诊断错误：" + json.dumps(error, ensure_ascii=False))
    lines += [
        "",
        "全部 AgentRun 与主循环事件用量：",
        json.dumps(report.get("model_usage", {}), ensure_ascii=False, indent=2),
    ]
    lines.append(
        "摘要模型调用可能计入AgentRun用量，但不在主循环model_request_completed事件计数中。"
    )
    return "\n".join(lines) + "\n"


async def main(args):
    started = time.perf_counter()
    report = {
        "report_kind": "current_backend_original_generation_recheck",
        "started_at_utc": utc_now(),
        "source_before": source_snapshot(),
        "source_provenance_notice": "磁盘源码指纹；执行前由主代理刷新受控backend，健康接口不提供已加载源码hash。",
        "base_url": args.base_url,
        "sessions": {},
        "cases": [],
        "complete": False,
        "scope": {
            "new_evaluation_profiles": 2,
            "concurrency": 1,
            "http_timeout_seconds": 720,
            "http_retry_count": 0,
            "production_budget_changed": False,
            "mathematical_reference_or_solver_injected": False,
            "business_schema_or_prompt_modified": False,
            "raw_outputs_are_developer_debug_material": True,
            "student_gain_or_global_quality_estimated": False,
        },
    }

    def save():
        report["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        args.transcript.write_text(developer_transcript(report), encoding="utf-8")

    search = SearchClient()
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        timeout=httpx.Timeout(720, connect=10),
        trust_env=False,
    ) as client:
        try:
            health = await client.get("/health")
            health.raise_for_status()
            public_health = health.json()
            assert public_health["application"] == "stem-tutor"
            report["health"] = public_health
            for case_id, text, expected_type in CASES:
                case_started = time.perf_counter()
                case = {
                    "case_id": case_id,
                    "user_message": text,
                    "expected_question_type": expected_type,
                    "student_id": "evaluation_" + uuid4().hex[:12],
                    "request_body": {
                        "content": text,
                        "intent": "practice",
                        "generate": True,
                        "exercise_count": 1,
                        "cognitive_type": "application",
                        "concept_ids": ["math:c175"],
                        "request_id": str(uuid4()),
                    },
                    "execution_status": "running",
                    "generation_status": "not_yet_observed",
                    "verification_status": "pending",
                    "errors": [],
                    "sse_frames": [],
                    "sse_comments": [],
                    "teacher_messages": [],
                }
                report["cases"].append(case)
                save()
                print(f"START {case_id}", flush=True)
                try:
                    response = await client.post(
                        "/sessions",
                        json={
                            "course_id": "math",
                            "student_id": case["student_id"],
                            "goal": text,
                            "concept_ids": ["math:c175"],
                        },
                    )
                    response.raise_for_status()
                    session_id = response.json()["id"]
                    case["session_id"] = session_id
                    report["sessions"][case_id] = session_id
                    save()
                    stream_started = time.perf_counter()
                    event_name, event_id, event_data = "message", None, []
                    async with client.stream(
                        "POST",
                        f"/sessions/{session_id}/messages?stream=true",
                        json=case["request_body"],
                    ) as stream:
                        case["http_status"] = stream.status_code
                        stream.raise_for_status()
                        assert stream.headers.get("content-type", "").startswith(
                            "text/event-stream"
                        )
                        async for line in stream.aiter_lines():
                            elapsed = round(time.perf_counter() - stream_started, 6)
                            if not line:
                                if event_data:
                                    data = json.loads("\n".join(event_data))
                                    case["sse_frames"].append(
                                        {
                                            "id": event_id,
                                            "event": event_name,
                                            "data": data,
                                            "elapsed_seconds": elapsed,
                                        }
                                    )
                                    if (
                                        event_name == "message"
                                        and data.get("role") == "assistant"
                                    ):
                                        case["teacher_messages"].append(data)
                                    if event_name in {
                                        "exercise_quality_checked",
                                        "agent_completed",
                                        "error",
                                        "done",
                                    }:
                                        print(
                                            f"{case_id} {event_name} {elapsed:.2f}s",
                                            flush=True,
                                        )
                                        save()
                                event_name, event_id, event_data = "message", None, []
                            elif line.startswith(":"):
                                case["sse_comments"].append(
                                    {"text": line, "elapsed_seconds": elapsed}
                                )
                            else:
                                field, _, value = line.partition(":")
                                value = value[1:] if value.startswith(" ") else value
                                if field == "event":
                                    event_name = value
                                elif field == "id":
                                    event_id = value
                                elif field == "data":
                                    event_data.append(value)
                    assert not event_data, "SSE在完整事件结束前关闭"
                    error_frames = [
                        frame
                        for frame in case["sse_frames"]
                        if frame["event"] == "error"
                    ]
                    done = [
                        frame
                        for frame in case["sse_frames"]
                        if frame["event"] == "done"
                    ]
                    if error_frames:
                        case["execution_status"] = "sse_error"
                        case["errors"].extend(
                            {"kind": "sse_error", "data": frame["data"]}
                            for frame in error_frames
                        )
                    elif done and done[-1]["data"].get("status") == "completed":
                        case["execution_status"] = "completed"
                    else:
                        case["execution_status"] = "incomplete_stream"
                    case["stream_duration_seconds"] = round(
                        time.perf_counter() - stream_started, 3
                    )
                except Exception as exc:
                    case["execution_status"] = "http_or_transport_error"
                    case["errors"].append(
                        {
                            "kind": "http_or_transport_error",
                            "type": type(exc).__name__,
                            "message": str(exc)[:600],
                        }
                    )
                finally:
                    if case.get("session_id"):
                        try:
                            audit = read_private_audit(case["session_id"])
                            case["private_audit"] = audit
                            snapshot_response = await client.get(
                                f"/sessions/{case['session_id']}"
                            )
                            snapshot_response.raise_for_status()
                            snapshot = snapshot_response.json()
                            case["public_session_snapshot"] = snapshot
                            published_ids = public_question_ids(snapshot)
                            attempts_by_id = {
                                item["audit_id"]: item for item in audit["attempts"]
                            }
                            runs_by_id = {
                                run["run_id"]: run for run in audit["agent_runs"]
                            }
                            checks = []
                            for question in audit["saved_questions"]:
                                validation = question["validation"]
                                attempt = attempts_by_id.get(
                                    validation.get("generation_attempt_id")
                                )
                                review = stage_output(
                                    (attempt or {}).get("stages", {}).get("review")
                                )
                                generation = stage_output(
                                    (attempt or {}).get("stages", {}).get("generation")
                                )
                                stage_runs = {
                                    key: (
                                        (attempt or {}).get("stages", {}).get(key) or {}
                                    ).get("run_id")
                                    for key in ("generation", "independent", "review")
                                }
                                item = {
                                    "question_id": question["id"],
                                    "published": question["id"] in published_ids,
                                    "generated_true": question["generated"] is True,
                                    "source_ref_matches_generation": question[
                                        "source_ref"
                                    ]
                                    == "习题 Agent 生成；经独立求解与模型语义复核",
                                    "quality_method_matches": validation.get(
                                        "quality_method"
                                    )
                                    == "semantic_reconciliation_v1",
                                    "accepted_audit_exists": bool(
                                        attempt and attempt["status"] == "accepted"
                                    ),
                                    "accepted_review_verdict": review.get("verdict")
                                    in {"equivalent", "complementary"},
                                    "answer_equals_accepted_review_answer": question[
                                        "answer"
                                    ]
                                    == review.get("answer"),
                                    "answer_source": "Question.answer is copied from accepted semantic review output.answer; it is not a parser-derived or injected reference answer",
                                    "explanation_equals_accepted_review": question[
                                        "explanation"
                                    ]
                                    == review.get("explanation"),
                                    "stem_equals_generation": question["stem"]
                                    == generation.get("stem"),
                                    "question_type_matches_request": question[
                                        "question_type"
                                    ]
                                    == expected_type,
                                    "three_stage_run_ids": stage_runs,
                                    "three_stage_runs_completed": all(
                                        identifier in runs_by_id
                                        and runs_by_id[identifier]["status"]
                                        == "completed"
                                        for identifier in stage_runs.values()
                                    ),
                                }
                                es = await search.request(
                                    "GET",
                                    f"/{search.QUESTIONS}/_doc/{quote(question['id'], safe='')}",
                                    params={"_source_excludes": "embedding"},
                                )
                                source = es.get("_source") or {}
                                item["es_document"] = es
                                item["es_found"] = es.get("found") is True
                                item["es_course_and_origin_match"] = (
                                    source.get("course_id") == "math"
                                    and source.get("origin") == "llm_generated"
                                )
                                item["es_stem_indexed"] = question[
                                    "stem"
                                ] in source.get("text", "")
                                item["es_private_answer_fields_absent"] = not {
                                    "answer",
                                    "explanation",
                                }.intersection(source)
                                required = [
                                    value
                                    for key, value in item.items()
                                    if isinstance(value, bool) and key != "published"
                                ]
                                item["saved_record_checks_passed"] = all(required)
                                checks.append(item)
                            case["question_checks"] = checks
                            published = [item for item in checks if item["published"]]
                            if published:
                                case["generation_status"] = "published"
                                case["verification_status"] = (
                                    "passed"
                                    if len(published) == 1
                                    and all(
                                        item["saved_record_checks_passed"]
                                        for item in published
                                    )
                                    else "failed"
                                )
                            elif checks:
                                case["generation_status"] = "saved_but_unpublished"
                                case["verification_status"] = "failed"
                            elif audit["attempts"] and all(
                                item["status"] == "rejected"
                                for item in audit["attempts"]
                            ):
                                case["generation_status"] = "quality_rejected"
                                case["verification_status"] = (
                                    "not_applicable_no_published_question"
                                )
                            else:
                                case["generation_status"] = "not_generated"
                                case["verification_status"] = (
                                    "not_applicable_no_published_question"
                                )
                            case["session_released"] = (
                                audit["session"]["active_turn_id"] is None
                            )
                            case["tool_error_kind_counts"] = dict(
                                Counter(
                                    trace.get("error_kind", "unspecified")
                                    for run in audit["agent_runs"]
                                    for trace in run["tool_calls"]
                                    if trace.get("status") in {"failed", "rejected"}
                                )
                            )
                            case["event_counts"] = dict(
                                Counter(frame["event"] for frame in case["sse_frames"])
                            )
                        except Exception as exc:
                            case["verification_status"] = "audit_error"
                            case["errors"].append(
                                {
                                    "kind": "audit_error",
                                    "type": type(exc).__name__,
                                    "message": str(exc)[:600],
                                }
                            )
                    case["duration_seconds"] = round(
                        time.perf_counter() - case_started, 3
                    )
                    save()
                print(
                    f"RESULT {case_id} execution={case['execution_status']} generation={case['generation_status']} verification={case['verification_status']}",
                    flush=True,
                )
        finally:
            await search.close()
            if report["sessions"]:
                try:
                    capture_all_run_usage(report)
                    report["usage_scope_notice"] = (
                        "All AgentRun records in both new sessions are aggregated. Context-summary model calls can contribute to AgentRun tokens, but are not individually counted in main-loop model_request_completed events. Missing event metrics are not assumed to be zero."
                    )
                except Exception as exc:
                    report["usage_audit_error"] = {
                        "type": type(exc).__name__,
                        "message": str(exc)[:600],
                    }
            report["source_after"] = source_snapshot()
            report["source_unchanged_during_run"] = (
                report["source_before"] == report["source_after"]
            )
            report["finished_at_utc"] = utc_now()
            report["complete"] = len(report["cases"]) == 2 and all(
                case["execution_status"] != "running" for case in report["cases"]
            )
            report["summary"] = {
                "cases_executed": len(report["cases"]),
                "technical_execution_completed": sum(
                    case["execution_status"] == "completed" for case in report["cases"]
                ),
                "generation_published": sum(
                    case["generation_status"] == "published" for case in report["cases"]
                ),
                "cases_ending_in_quality_rejection": sum(
                    case["generation_status"] == "quality_rejected"
                    for case in report["cases"]
                ),
                "all_generation_checks_passed": len(report["cases"]) == 2
                and all(
                    case["verification_status"] == "passed" for case in report["cases"]
                ),
                "generation_rejection_is_not_a_technical_execution_failure": True,
                "generation_attempts_total": sum(
                    len(case.get("private_audit", {}).get("attempts", []))
                    for case in report["cases"]
                ),
                "generation_attempts_rejected": sum(
                    attempt["status"] == "rejected"
                    for case in report["cases"]
                    for attempt in case.get("private_audit", {}).get("attempts", [])
                ),
                "generation_attempts_accepted": sum(
                    attempt["status"] == "accepted"
                    for case in report["cases"]
                    for attempt in case.get("private_audit", {}).get("attempts", [])
                ),
                "mathematical_correctness_checked_by_this_script": False,
            }
            save()
    print(
        json.dumps(
            {
                "report": str(args.report),
                "transcript": str(args.transcript),
                **report["summary"],
                "model_usage": report.get("model_usage"),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return (
        0
        if report["complete"]
        and report["summary"]["technical_execution_completed"] == 2
        else 1
    )


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8008/api")
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "data/reports/current_model_generation_recheck.json",
    )
    parser.add_argument(
        "--transcript",
        type=Path,
        default=ROOT / "data/reports/current_model_generation_recheck.txt",
    )
    raise SystemExit(asyncio.run(main(parser.parse_args())))
