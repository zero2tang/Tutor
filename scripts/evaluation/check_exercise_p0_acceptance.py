"""普通问法的 P0 出题验收；失败原样保留，数学核验留待事后人工完成。"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import sys
import time
from urllib.parse import quote
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.infrastructure.clients.llm import ModelClient
from app.infrastructure.clients.search import SearchClient
from app.infrastructure.persistence.database import db_session
from app.application.agent.runner import AgentRunner
from app.domains.practice.schemas import SolutionCheck
from app.domains.practice.reviews import ACCEPTED_VERDICTS, ExerciseReviewService
from app.agent.tools.contracts import ToolContext
from app.application.agent.tools import TeachingTools
from check_current_model_generation import (
    developer_transcript,
    public_question_ids,
    read_private_audit,
    source_snapshot,
    stage_output,
    utc_now,
)
from natural_teaching_audit import capture_all_run_usage

CASES = (
    ("hard_fill", "请给我出一道原创、比较难的和差倍应用题，做成单空填空题。", "fill"),
    (
        "ordinary_choice",
        "请给我出一道原创的和差倍应用题，普通难度，做成单选题。",
        "choice",
    ),
    (
        "continuous_fill",
        "请给我出一道关于粮食重量的原创和差倍填空题，答案可以是分数。",
        "fill",
    ),
)


def no_private_fields(value):
    """检查公开结构字段；这不替代对教师正文的事后人工核查。"""
    if isinstance(value, dict):
        forbidden = {
            "answer",
            "explanation",
            "generation_solution",
            "independent_solution",
            "generation_assessment",
            "independent_assessment",
            "_exercise_contract",
        }
        return not forbidden.intersection(value) and all(
            no_private_fields(item) for item in value.values()
        )
    if isinstance(value, list):
        return all(no_private_fields(item) for item in value)
    return True


async def receive_stream(client, sid, case, save):
    """按实际收到的 SSE 保留正文和阶段事件，不主动重试 HTTP。"""
    started = time.perf_counter()
    name, identifier, lines = "message", None, []
    async with client.stream(
        "POST", f"/sessions/{sid}/messages?stream=true", json=case["request_body"]
    ) as response:
        case["http_status"] = response.status_code
        response.raise_for_status()
        assert response.headers.get("content-type", "").startswith("text/event-stream")
        async for line in response.aiter_lines():
            elapsed = round(time.perf_counter() - started, 6)
            if not line:
                if lines:
                    data = json.loads("\n".join(lines))
                    case["sse_frames"].append(
                        {
                            "id": identifier,
                            "event": name,
                            "data": data,
                            "elapsed_seconds": elapsed,
                        }
                    )
                    if name == "message" and data.get("role") == "assistant":
                        case["teacher_messages"].append(data)
                    if name in {
                        "exercise_quality_checked",
                        "agent_completed",
                        "error",
                        "done",
                    }:
                        print(case["case_id"], name, round(elapsed, 2), flush=True)
                        save()
                name, identifier, lines = "message", None, []
            elif line.startswith(":"):
                case["sse_comments"].append(
                    {"content": line, "elapsed_seconds": elapsed}
                )
            else:
                field, _, value = line.partition(":")
                value = value[1:] if value.startswith(" ") else value
                if field == "event":
                    name = value
                elif field == "id":
                    identifier = value
                elif field == "data":
                    lines.append(value)
    assert not lines, "SSE未提交完整末帧"
    errors = [
        frame["data"] for frame in case["sse_frames"] if frame["event"] == "error"
    ]
    done = [frame for frame in case["sse_frames"] if frame["event"] == "done"]
    case["execution_status"] = (
        "completed"
        if done and not errors
        else "sse_error" if errors else "incomplete_stream"
    )
    case["errors"].extend({"kind": "sse_error", "data": item} for item in errors)


async def audit_api_case(client, search, case):
    """答案只核对保存来源与协议；不在脚本内求解或更换答案。"""
    audit = read_private_audit(case["session_id"])
    case["private_audit"] = audit
    response = await client.get(f"/sessions/{case['session_id']}")
    response.raise_for_status()
    snapshot = response.json()
    case["public_session_snapshot"] = snapshot
    case["public_private_boundary"] = {
        "session_snapshot_has_no_private_answer_fields": no_private_fields(snapshot),
        "sse_has_no_private_answer_fields": no_private_fields(case["sse_frames"]),
        "teacher_body_review": "pending; key checks do not prove semantic non-disclosure",
    }
    published_ids = public_question_ids(snapshot)
    by_id = {item["audit_id"]: item for item in audit["attempts"]}
    checks = []
    for question in audit["saved_questions"]:
        attempt = by_id[question["validation"]["generation_attempt_id"]]
        stages = attempt["stages"]
        generation, blind, review = (
            stage_output(stages.get(key))
            for key in ("generation", "independent", "review")
        )
        assessments = [
            (review.get("generation_assessment") or {}, generation),
            (review.get("independent_assessment") or {}, blind),
        ]
        es = await search.request(
            "GET",
            f"/{search.QUESTIONS}/_doc/{quote(question['id'], safe='')}",
            params={"_source_excludes": "embedding"},
        )
        source = es.get("_source") or {}
        item = {
            "question_id": question["id"],
            "published": question["id"] in published_ids,
            "generated_true": question["generated"] is True,
            "question_type_matches_user_request": question["question_type"]
            == case["expected_question_type"],
            "quality_method_v2": question["validation"].get("quality_method")
            == "semantic_reconciliation_v2",
            "source_ref": question["source_ref"],
            "audit_accepted": attempt["status"] == "accepted",
            "review_accepted": review.get("verdict") in ACCEPTED_VERDICTS,
            "answer_equals_accepted_review": question["answer"] == review.get("answer"),
            "answer_source": "accepted review.answer copied to Question.answer; no reference or mathematical parser supplied by this script",
            "explanation_equals_accepted_review": question["explanation"]
            == review.get("explanation"),
            "submitted_answer_receipts_match": all(
                item.get("submitted_answer") == original.get("answer")
                for item, original in assessments
            ),
            "both_assessments_correct_consistent": all(
                item.get("correctness") == "correct"
                and item.get("answer_explanation_consistency") == "consistent"
                for item, _ in assessments
            ),
            "es_found": es.get("found") is True,
            "es_stem_matches": question["stem"] in source.get("text", ""),
            "es_origin_matches": source.get("course_id") == "math"
            and source.get("origin") == "llm_generated",
            "es_private_fields_absent": not {"answer", "explanation"}.intersection(
                source
            ),
            "es_document": es,
        }
        item["passed"] = all(
            value for value in item.values() if isinstance(value, bool)
        )
        checks.append(item)
    case["question_checks"] = checks
    case["generation_status"] = (
        "published"
        if any(item["published"] for item in checks)
        else (
            "saved_but_unpublished"
            if checks
            else "quality_rejected" if audit["attempts"] else "not_generated"
        )
    )
    case["verification_status"] = (
        "passed"
        if len(checks) == 1 and checks[0]["passed"]
        else "failed" if checks else "not_applicable_no_publication"
    )
    case["session_released"] = audit["session"]["active_turn_id"] is None
    case["manual_math_review"] = {
        "status": "pending",
        "candidate_count": len(audit["attempts"]),
        "published_count": len(checks),
    }


async def fixed_fraction_probe(client, report, save):
    """固定旧题只输入题干，两个真实盲解后交真实复核服务；不是 API 新题发布。"""
    historical = json.loads(
        (ROOT / "data/reports/exercise_generation_diagnosis.json").read_text(
            encoding="utf-8"
        )
    )
    row = next(
        item
        for item in historical["six_attempts"]
        if item["audit_id"] == "2c26edc0-ead7-486f-9487-a61180656c30"
    )
    stem = row["raw_stages"]["generation"]["output"]["stem"]
    probe = {
        "kind": "fixed_historical_item_two_blind_runs_then_real_review_service",
        "not_an_api_generation_case": True,
        "source_audit_id": row["audit_id"],
        "stem": stem,
        "reference_answers_in_requests": False,
        "events": [],
        "solutions": [],
        "status": "running",
    }
    report["fixed_fraction_probe"] = probe
    student = "evaluation_" + uuid4().hex[:12]
    response = await client.post(
        "/sessions",
        json={
            "course_id": "math",
            "student_id": student,
            "goal": "开发者历史固定连续量题的独立求解和复核诊断",
            "concept_ids": ["math:c175"],
        },
    )
    response.raise_for_status()
    sid, turn_id = response.json()["id"], str(uuid4())
    report["sessions"]["fixed_fraction_probe"] = sid
    probe.update(session_id=sid, turn_id=turn_id)
    sequence = 0

    async def emit(event, data):
        nonlocal sequence
        sequence += 1
        probe["events"].append(
            {"event": event, "seq": sequence, "data": deepcopy(data)}
        )
        # 直接调用的事件只保存在当前验收报告，不重建 SQL 临时事件。
        save()

    models = ModelClient()
    runner = AgentRunner(models, TeachingTools(None))
    context = ToolContext(
        course_id="math", session_id=sid, student_id=student, concept_ids=["math:c175"]
    )
    payload = {
        "task": "validate",
        "stem": stem,
        "options": {},
        "question_type": "fill",
        "concept_ids": ["math:c175"],
        "instruction": "独立求解并判断唯一可解性，不知道生成者参考答案。",
    }
    probe["actual_blind_payload"] = payload
    try:
        for _ in range(2):
            result = await runner.run(
                "exercise",
                "application",
                deepcopy(payload),
                SolutionCheck,
                context,
                turn_id,
                emit=emit,
                use_tools=False,
            )
            probe["solutions"].append(result)
            save()
        first, second = [item["output"] for item in probe["solutions"]]
        draft = {
            "stem": stem,
            "options": {},
            "question_type": "fill",
            "concept_ids": ["math:c175"],
            "cognitive_type": "application",
            "answer": first["answer"],
            "explanation": first["explanation"],
        }
        probe["review_input_notice"] = (
            "generation_solution is solution A from the first independent call, not a hidden reference or a generated new item"
        )
        probe["review"] = await ExerciseReviewService(runner).reconcile(
            draft, second, context, turn_id, emit
        )
        probe["status"] = "completed"
        probe["manual_validity_and_math_review"] = (
            "pending; do not equate model acceptance with mathematical proof"
        )
    except Exception as exc:
        probe.update(
            status="failed",
            error={"type": type(exc).__name__, "message": str(exc)[:600]},
        )
    finally:
        await models.close()
        probe["private_audit"] = read_private_audit(sid)
        save()


async def main(args):
    started = time.perf_counter()
    report = {
        "report_kind": "exercise_p0_real_backend_acceptance",
        "started_at_utc": utc_now(),
        "source_before": source_snapshot(),
        "sessions": {},
        "cases": [],
        "complete": False,
        "scope": {
            "api_generation_cases": 3,
            "fixed_service_probe": not args.skip_fixed_probe,
            "http_timeout_seconds": 720,
            "http_retries": 0,
            "concurrency": 1,
            "no_hidden_mathematical_answers": True,
            "no_parser_or_solver_injected": True,
            "default_agent_routing_preserved": True,
            "source_provenance": "disk fingerprints; root must refresh the managed backend before execution",
        },
    }

    def save():
        report["wall_seconds"] = round(time.perf_counter() - started, 3)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        text = developer_transcript(report)
        if report.get("fixed_fraction_probe"):
            text += (
                "\n固定旧题直接服务探针（非API新用户题）：\n"
                + json.dumps(
                    report["fixed_fraction_probe"], ensure_ascii=False, indent=2
                )
                + "\n"
            )
        args.transcript.write_text(text, encoding="utf-8")

    search = SearchClient()
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        timeout=httpx.Timeout(720, connect=10),
        trust_env=False,
    ) as client:
        try:
            for key, text, kind in CASES:
                case_started = time.perf_counter()
                case = {
                    "case_id": key,
                    "user_message": text,
                    "expected_question_type": kind,
                    "request_body": {
                        "content": text,
                        "concept_ids": ["math:c175"],
                        "request_id": str(uuid4()),
                    },
                    "execution_status": "running",
                    "generation_status": "pending",
                    "verification_status": "pending",
                    "sse_frames": [],
                    "sse_comments": [],
                    "teacher_messages": [],
                    "errors": [],
                }
                report["cases"].append(case)
                save()
                try:
                    student = "evaluation_" + uuid4().hex[:12]
                    response = await client.post(
                        "/sessions",
                        json={
                            "course_id": "math",
                            "student_id": student,
                            "goal": text,
                            "concept_ids": ["math:c175"],
                        },
                    )
                    response.raise_for_status()
                    case.update(session_id=response.json()["id"], student_id=student)
                    report["sessions"][key] = case["session_id"]
                    print("START", key, flush=True)
                    await receive_stream(client, case["session_id"], case, save)
                except Exception as exc:
                    case.update(execution_status="technical_error")
                    case["errors"].append(
                        {
                            "kind": "technical_error",
                            "type": type(exc).__name__,
                            "message": str(exc)[:600],
                        }
                    )
                finally:
                    if case.get("session_id"):
                        try:
                            await audit_api_case(client, search, case)
                        except Exception as exc:
                            case["errors"].append(
                                {
                                    "kind": "audit_error",
                                    "type": type(exc).__name__,
                                    "message": str(exc)[:600],
                                }
                            )
                            case["verification_status"] = "audit_error"
                    case["duration_seconds"] = round(
                        time.perf_counter() - case_started, 3
                    )
                    save()
                print(
                    "RESULT",
                    key,
                    case["execution_status"],
                    case["generation_status"],
                    case["verification_status"],
                    flush=True,
                )
            if not args.skip_fixed_probe:
                try:
                    await fixed_fraction_probe(client, report, save)
                except Exception as exc:
                    report.setdefault("fixed_fraction_probe", {}).update(
                        status="failed",
                        error={"type": type(exc).__name__, "message": str(exc)[:600]},
                    )
        finally:
            await search.close()
            capture_all_run_usage(report)
            report["requested_models_from_agent_runs"] = report.pop("actual_models", [])
            report["response_models"] = sorted(
                {
                    item["response_model"]
                    for item in report.get("model_requests", [])
                    if item.get("response_model")
                }
            )
            report["response_model_notice"] = (
                "unknown when response_model is absent; model and AgentRun.model identify the requested model"
            )
            report["upstream_finish_reasons"] = (
                "unknown; current main-loop events do not preserve provider finish_reason"
            )
            report["usage_scope_notice"] = (
                "AgentRun totals cover three API sessions plus the separate direct-service probe. Main-loop events count actual emitted requests; summary model calls may add AgentRun tokens without a main-loop event. API task and direct probe denominators remain separate."
            )
            report["before_comparison_notice"] = (
                "The first two user texts match the previous recheck. The previous HTTP body forced intent=practice/generate=true; this run leaves those defaults untouched. Cost changes are descriptive and cannot be attributed solely to P0 changes."
            )
            attempts = [
                item
                for case in report["cases"]
                for item in case.get("private_audit", {}).get("attempts", [])
            ]
            report["summary"] = {
                "api_tasks": len(report["cases"]),
                "api_tasks_published": sum(
                    case["generation_status"] == "published" for case in report["cases"]
                ),
                "api_protocol_checks_passed": sum(
                    case["verification_status"] == "passed" for case in report["cases"]
                ),
                "candidate_attempts": len(attempts),
                "candidate_status_counts": dict(
                    Counter(item["status"] for item in attempts)
                ),
                "manual_math_review": "pending for all candidates, publications and fraction false-rejection probe",
            }
            report["source_after"] = source_snapshot()
            report["source_unchanged_during_run"] = (
                report["source_before"] == report["source_after"]
            )
            report["complete"] = len(report["cases"]) == 3 and all(
                case["execution_status"] != "running" for case in report["cases"]
            )
            report["finished_at_utc"] = utc_now()
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
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8008/api")
    parser.add_argument(
        "--report", type=Path, default=ROOT / "data/reports/exercise_p0_acceptance.json"
    )
    parser.add_argument(
        "--transcript",
        type=Path,
        default=ROOT / "data/reports/exercise_p0_acceptance.txt",
    )
    parser.add_argument("--skip-fixed-probe", action="store_true")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
