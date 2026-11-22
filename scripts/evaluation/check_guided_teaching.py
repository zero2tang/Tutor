"""真实 HTTP→MySQL→Agent→工具→SSE 的通用引导教学验收。

外部新题与学生回答均为明确自编的验收情境，不代表真实教学效果。
"""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

import httpx
from request_protocol import request_body as native_request_body

_request_bindings = {}

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))


def source_digest():
    digest = hashlib.sha256()
    paths = [
        *(ROOT / "backend/app").rglob("*.py"),
        *(ROOT / "backend/app/agent/skills").rglob("*.md"),
    ]
    for path in sorted(paths):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def assert_public(value):
    """公共教学状态不应包含内部参考解或完整分析工作区。"""
    if isinstance(value, dict):
        assert not {
            "reference_solution",
            "teaching_workspace",
            "expected_answer",
            "_guidance_response_context",
            "_guidance_receipt",
        }.intersection(value)
        for part in value.values():
            assert_public(part)
    elif isinstance(value, list):
        for part in value:
            assert_public(part)


def frames(text):
    result = []
    for frame in text.replace("\r\n", "\n").split("\n\n"):
        event = next(
            (
                line[6:].strip()
                for line in frame.splitlines()
                if line.startswith("event:")
            ),
            "",
        )
        data = "\n".join(
            line[5:].strip() for line in frame.splitlines() if line.startswith("data:")
        )
        if event and data:
            result.append((event, json.loads(data)))
    return result


def raw_workspace(session_id):
    """验收读取持久化工作区，私有参考解只用于模拟协议作答。"""
    from app.infrastructure.persistence.database import db_session
    from app.domains.sessions.models import LearningSession

    with db_session() as db:
        row = db.get(LearningSession, session_id)
        from app.domains.guidance.history import hydrate
        return hydrate(db,deepcopy(row.pending_context["guided_teaching"]))


def capture_runtime_audit(report):
    """失败时也保留真实模型用量和上游重试指标，不把工具轮次叫作重试。"""
    from sqlalchemy import select
    from app.infrastructure.persistence.database import db_session
    from app.infrastructure.persistence.execution_models import AgentRun
    from diagnostic_events import read_diagnostic_events

    identifiers = list(report["sessions"].values())
    if not identifiers:
        return
    with db_session() as db:
        report["database_dialect"] = db.get_bind().dialect.name
        runs = list(
            db.scalars(
                select(AgentRun).where(
                    AgentRun.session_id.in_(identifiers),
                    AgentRun.skill == "guided_explanation",
                )
            )
        )
        report["actual_models"] = sorted({run.model for run in runs})
        report["run_outcomes"] = [
            {
                "id": run.id,
                "session_id": run.session_id,
                "status": run.status,
                "error": run.error,
            }
            for run in runs
        ]
        model_requests = [
            {"event_id": event.id, "turn_id": event.turn_id, **event.data}
            for event in read_diagnostic_events(identifiers)
            if event.event == "model_request_completed"
        ]
        report["model_requests"] = model_requests
        attempts = [
            item["request_attempts"]
            for item in model_requests
            if item.get("request_attempts") is not None
        ]
        report["model_usage"] = {
            "run_count": len(runs),
            "failed_run_count": sum(run.status != "completed" for run in runs),
            "prompt_tokens": sum(run.prompt_tokens or 0 for run in runs),
            "completion_tokens": sum(run.completion_tokens or 0 for run in runs),
            "model_duration_ms": round(sum(run.duration_ms or 0 for run in runs), 2),
            "completed_model_request_count": len(model_requests),
            "upstream_request_attempts": sum(attempts),
            "upstream_retry_count": sum(max(0, value - 1) for value in attempts),
            "requests_missing_attempt_metrics": len(model_requests) - len(attempts),
            "http_retry_count": 0,
        }


async def main(args):
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "backend_source_sha256": source_digest(),
        "acceptance_script_sha256": hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest(),
        "student_id": "evaluation_" + uuid4().hex[:12],
        "data_origin": {
            "problems": "self_authored_external_problem_fixtures",
            "student_responses": "simulated",
            "course_resources": "existing_course_database",
        },
        "real_services": [
            "HTTP",
            "MySQL",
            "AgentRunner",
            "configured_LLM",
            "read_only_tools",
            "SSE",
        ],
        "execution": {
            "concurrency": 1,
            "request_timeout_seconds": args.timeout,
            "http_retry_policy": "none; idempotent replay is tested explicitly",
            "retry_metrics_scope": "persisted completed model requests; unreported terminal attempts remain unknown",
        },
        "not_tested": [
            "真实学生学习增益",
            "长期保持",
            "教师诊断一致性",
            "图片识别质量",
            "全学科解题正确率",
            "掌握概率校准",
        ],
        "checks": [],
        "requests": [],
        "sessions": {},
        "complete": False,
        "failure_count": 0,
        "transport_failure_count": 0,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"), timeout=args.timeout, trust_env=False
    ) as client:

        async def request(method, path, body=None, expected=200, sse=False):
            body = await native_request_body(client, method, path, body, _request_bindings)
            started = time.perf_counter()
            metrics = {"method": method, "path": path, "expected_status": expected}
            report["requests"].append(metrics)
            try:
                if sse:
                    chunks = []
                    tail = ""
                    async with client.stream(method, path, json=body) as response:
                        metrics["status"] = response.status_code
                        async for chunk in response.aiter_text():
                            if chunk and "first_bytes_ms" not in metrics:
                                metrics["first_bytes_ms"] = round(
                                    (time.perf_counter() - started) * 1000, 2
                                )
                            event_window = tail + chunk
                            if (
                                "event: text_delta" in event_window
                                and "first_text_delta_ms" not in metrics
                            ):
                                metrics["first_text_delta_ms"] = round(
                                    (time.perf_counter() - started) * 1000, 2
                                )
                            tail = event_window[-64:]
                            chunks.append(chunk)
                    text = "".join(chunks)
                else:
                    response = await client.request(method, path, json=body)
                    metrics["status"] = response.status_code
                    text = response.text
            except Exception as exc:
                metrics["transport_error"] = f"{type(exc).__name__}: {exc}"
                report["transport_failure_count"] += 1
                raise
            finally:
                metrics["duration_ms"] = round(
                    (time.perf_counter() - started) * 1000, 2
                )
            assert (
                response.status_code == expected
            ), f"{method} {path}: {response.status_code} {text[:800]}"
            if sse:
                assert response.headers.get("content-type", "").startswith(
                    "text/event-stream"
                )
                events = frames(text)
                assert not any(name == "error" for name, _ in events), events
                assert any(name == "message" for name, _ in events)
                assert events[-1][0] == "done"
                report.setdefault("sse_event_names", []).append(
                    [name for name, _ in events]
                )
                metrics["text_delta_event_count"] = sum(
                    name == "text_delta" for name, _ in events
                )
                return events
            return json.loads(text)

        async def check(label, operation):
            started = time.perf_counter()
            try:
                details = await operation()
                report["checks"].append(
                    {
                        "label": label,
                        "status": "passed",
                        "details": details,
                        "duration_seconds": round(time.perf_counter() - started, 3),
                    }
                )
                print("PASS " + label, flush=True)
            except Exception as exc:
                report["failure_count"] += 1
                report["checks"].append(
                    {
                        "label": label,
                        "status": "failed",
                        "error": f"{type(exc).__name__}: {str(exc)[:1200]}",
                        "duration_seconds": round(time.perf_counter() - started, 3),
                    }
                )
                try:
                    capture_runtime_audit(report)
                except Exception as audit_error:
                    report["audit_error"] = (
                        f"{type(audit_error).__name__}: {audit_error}"
                    )
                report["wall_time_seconds"] = round(
                    time.perf_counter() - args.started, 3
                )
                save()
                raise
            save()

        async def session(course, goal, key=None):
            result = await request(
                "POST",
                "/sessions",
                {
                    "course_id": course,
                    "student_id": report["student_id"],
                    "goal": goal,
                    "concept_ids": [],
                },
            )
            report["sessions"][key or course] = result["id"]
            return result["id"]

        async def act(sid, action, text="", problem=None, sse=False):
            current = await request("GET", f"/sessions/{sid}")
            body = {
                "action": action,
                "text": text,
                "request_id": str(uuid4()),
                "expected_version": current["version"],
            }
            if problem is not None:
                body["problem"] = {"stem": problem, "source_kind": "recognized"}
            result = await request(
                "POST",
                f"/sessions/{sid}/guidance?stream={'true' if sse else 'false'}",
                body,
                sse=sse,
            )
            return result, body

        sid = await session(
            "junior_algebra", "自编验收情境：新应用题的建模、追问和渐进引导"
        )
        last_body = None

        async def start_application():
            nonlocal last_body
            result, last_body = await act(
                sid,
                "start",
                problem="【自编验收题；假设图片已准确识别】班级购买若干本相同的练习本，每本3元，另付6元运费，一共花42元。购买了多少本？先引导我找数量关系，请结合当前课程可用教材资料。",
            )
            guidance = result["guidance"]
            assert guidance["problem"] and guidance["pending_question"]
            assert guidance["turn_count"] >= 1
            assert_public(guidance)
            assert result["message"]["content"].strip()
            return {
                "status": guidance["status"],
                "problem": guidance["problem"],
                "message": result["message"]["content"],
            }

        await check(
            "G01 新的一元方程应用题进入Agent引导，不要求预置答案或规则题型",
            start_application,
        )

        async def retry():
            before = await request("GET", f"/sessions/{sid}/runs")
            workspace_before = raw_workspace(sid)
            snapshot_before = await request("GET", f"/sessions/{sid}")
            first = await request(
                "POST", f"/sessions/{sid}/guidance?stream=false", last_body
            )
            second = await request(
                "POST", f"/sessions/{sid}/guidance?stream=false", last_body
            )
            after = await request("GET", f"/sessions/{sid}/runs")
            assert first["replayed"] and second["replayed"]
            assert first["message"]["id"] == second["message"]["id"]
            assert before == after
            assert raw_workspace(sid) == workspace_before
            snapshot_after = await request("GET", f"/sessions/{sid}")
            assert snapshot_after["version"] == snapshot_before["version"]
            assert snapshot_after["messages"] == snapshot_before["messages"]
            await request(
                "POST",
                f"/sessions/{sid}/guidance?stream=false",
                {**last_body, "text": "另一个请求内容"},
                expected=400,
            )
            current = await request("GET", f"/sessions/{sid}")
            await request(
                "POST",
                f"/sessions/{sid}/guidance?stream=false",
                {
                    "action": "reply",
                    "text": "过期页面输入",
                    "request_id": str(uuid4()),
                    "expected_version": max(0, current["version"] - 1),
                },
                expected=400,
            )
            return {
                "replayed_message_id": first["message"]["id"],
                "duplicate_model_calls": 0,
                "conflicting_request_rejected": True,
                "stale_version_rejected": True,
                "duplicate_evidence_count": 0,
            }

        await check("G02 幂等重放及冲突版本在模型调用前拦截", retry)

        async def followup():
            question = "为什么运费要单独减去？我不明白等式两边都减6的原因，请先解释这个疑问，再让我继续。"
            result = await request(
                "POST",
                f"/sessions/{sid}/messages?stream=false",
                {"content": question, "request_id": str(uuid4())},
            )
            assert result["guidance"]["turn_count"] >= 2
            current = await request("GET", f"/sessions/{sid}")
            assert current["guidance"] == result["guidance"]
            assert any(
                m["role"] == "user" and m["content"] == question
                for m in current["messages"]
            )
            assert_public(current["guidance"])
            return {
                "student_question": question,
                "agent_reply": result["message"]["content"],
                "restored_version": current["version"],
            }

        await check("G03 普通消息续接原任务，保留追问并从MySQL恢复", followup)

        transfer_answer_body = None
        independent_evidence = None
        simulated_transfer_answer = None

        async def help_and_transfer():
            nonlocal transfer_answer_body, independent_evidence
            nonlocal simulated_transfer_answer
            hinted, _ = await act(sid, "hint", "我还不确定怎么列式，请给局部提示。")
            assert hinted["guidance"]["assisted"]
            explained, _ = await act(
                sid,
                "explain",
                "请把数量关系和为什么这样列方程讲完整，再给我一道未讲过的新题。",
            )
            assert explained["guidance"]["assisted"]
            transfer, _ = await act(
                sid, "transfer", "请给一道新情境让我独立尝试，先不展示答案。"
            )
            assert transfer["guidance"]["transfer"]["stem"]
            assert_public(transfer["guidance"])
            # 只验证作答协议；模拟答案复制自私有参考解，不能用来证明学生学会了。
            before = raw_workspace(sid)
            assert before["transfer"]["assisted"] is False
            simulated_transfer_answer = before["transfer"]["reference_solution"]
            answer = (
                "我的正式解题作答如下：\n"
                + simulated_transfer_answer
                + "\n以上是我已经提交的作答。请在收到作答后完整解释这道新题的"
                "关键数量关系和求解依据，讲完自然停顿，不要附加问题、练习或新题。"
            )
            replied, transfer_answer_body = await act(sid, "reply", answer)
            workspace = raw_workspace(sid)
            evidence = workspace["evidence"][-1]
            assert evidence["raw_input"] == answer and evidence["transfer_response"]
            assert (
                evidence["assessment_method"] == "model_observation_not_program_proof"
            )
            assert evidence["student_input_kind"] == "attempt"
            assert evidence["is_attempt"] is True
            assert evidence["assisted_before_response"] is False
            assert evidence["independent_response"] is True
            context = evidence["response_context"]
            assert context["workspace_id"] == before["id"]
            assert context["workspace_version"] == before["version"]
            assert context["problem_id"] == before["transfer"]["id"]
            assert context["problem_kind"] == "transfer"
            assert context["assisted"] is False
            assert workspace["transfer"]["assisted"] is True
            assert replied["guidance"]["response_expectation"] == "none"
            assert replied["guidance"]["pending_question"] == ""
            independent_evidence = deepcopy(evidence)
            return {
                "assisted_original": True,
                "transfer_status": transfer["guidance"]["status"],
                "transfer_problem": transfer["guidance"]["transfer"]["stem"],
                "mastery_claimed": False,
                "transfer_response_recorded": True,
                "pre_response_context": context,
                "independent_before_teacher_feedback": evidence["independent_response"],
                "teacher_feedback_assists_future_responses": workspace["transfer"][
                    "assisted"
                ],
                "simulation_method": "answer_copied_from_private_reference_for_backend_protocol_test_only",
            }

        await check("G04 答后补讲保留本次独立证据，并仅影响后续作答", help_and_transfer)

        mid = await session("mechanics", "自编验收情境：路程与位移概念辨析，多轮追问")

        async def mechanics():
            events, _ = await act(
                mid,
                "start",
                problem="【自编验收题；文字条件完整】小明沿400米跑道走完一整圈，回到出发点。他走过的路程和位移大小分别是多少？请检索本课程中路程与位移的资料，逐步引导我分清两个概念。",
                sse=True,
            )
            restored = await request("GET", f"/sessions/{mid}")
            assert restored["guidance"]["pending_question"]
            assert_public(restored["guidance"])
            assert not restored.get("tutoring")
            return {
                "events": [name for name, _ in events],
                "pending_question": restored["guidance"]["pending_question"],
            }

        await check(
            "G05 非方程题经真实Agent及SSE引导，不经过固定方程核验门槛", mechanics
        )

        async def mechanics_followup():
            events = await request(
                "POST",
                f"/sessions/{mid}/messages?stream=true",
                {
                    "content": "我觉得位移也是400米，因为确实走了400米。为什么回到起点会影响位移？请用起点终点解释后问我一个小问题。",
                    "request_id": str(uuid4()),
                },
                sse=True,
            )
            current = await request("GET", f"/sessions/{mid}")
            assert current["guidance"]["turn_count"] >= 2
            assert_public(current["guidance"])
            return {
                "restored_turn_count": current["guidance"]["turn_count"],
                "events": [name for name, _ in events],
            }

        await check("G06 概念性错误与原因追问继续由教学Agent处理", mechanics_followup)

        async def complete_explanation_pause():
            result, _ = await act(
                mid,
                "explain",
                "请完整解释原题中路程与位移的定义、区别和答案依据。仅完整解释，"
                "不要附加问题、邀请作答或练习，也不要给新题；讲完自然停顿，保持可以继续追问。",
            )
            guidance = result["guidance"]
            assert guidance["response_expectation"] == "none"
            assert guidance["pending_question"] == ""
            assert guidance["status"] == "ready"
            assert guidance["closed"] is False
            assert not result["message"]["content"].rstrip().endswith(("?", "？"))
            assert_public(guidance)
            return {
                "response_expectation": guidance["response_expectation"],
                "status": guidance["status"],
                "message": result["message"]["content"],
                "workspace_still_active": not guidance["closed"],
            }

        await check(
            "G07 完整解释自然停顿，保持原工作区可继续追问", complete_explanation_pause
        )

        async def reason_explanation_pause():
            text = (
                "我继续追问原因：为什么回到起点时位移大小是零，但路程不是零？"
                "请只解释这个原因，不要附加问题、邀请作答或练习，讲完自然停顿。"
            )
            events = await request(
                "POST",
                f"/sessions/{mid}/messages?stream=true",
                {"content": text, "request_id": str(uuid4())},
                sse=True,
            )
            restored = await request("GET", f"/sessions/{mid}")
            guidance = restored["guidance"]
            assert guidance["response_expectation"] == "none"
            assert guidance["pending_question"] == ""
            assert guidance["status"] == "ready" and guidance["closed"] is False
            evidence = raw_workspace(mid)["evidence"][-1]
            assert evidence["raw_input"] == text
            assert evidence["student_input_kind"] == "question"
            assert evidence["is_attempt"] is False
            assert evidence["independent_response"] is False
            deltas = [data for name, data in events if name == "text_delta"]
            assert len(deltas) > 1, "尚未观察到多段真实模型正文增量"
            assistant_index = next(
                index
                for index, (name, data) in enumerate(events)
                if name == "message" and data.get("role") == "assistant"
            )
            assert any(name == "text_delta" for name, _ in events[:assistant_index])
            assert_public(guidance)
            return {
                "student_question": text,
                "response_expectation": guidance["response_expectation"],
                "text_delta_count": len(deltas),
                "student_input_kind": evidence["student_input_kind"],
                "agent_reply": events[assistant_index][1]["content"],
            }

        await check(
            "G08 自由追问原因经SSE解释，无强制追问且不冒充作答",
            reason_explanation_pause,
        )

        async def restore_and_replay_evidence():
            from app.infrastructure.persistence.database import db_session
            from app.domains.sessions.models import LearningSession, Message
            from app.domains.sessions.service import SessionService
            from sqlalchemy import select

            before = raw_workspace(sid)
            before_snapshot = await request("GET", f"/sessions/{sid}")
            before_runs = await request("GET", f"/sessions/{sid}/runs")
            # 重新构建会话服务，从MySQL恢复；不替换模型或注入工作区。
            restored = SessionService().get(sid)
            assert restored["guidance"] == before_snapshot["guidance"]
            first = await request(
                "POST", f"/sessions/{sid}/guidance?stream=false", transfer_answer_body
            )
            assert first["replayed"]
            replay_events = await request(
                "POST",
                f"/sessions/{sid}/guidance?stream=true",
                transfer_answer_body,
                sse=True,
            )
            assert not any(
                name in {"agent_started", "model_request_completed", "text_delta"}
                for name, _ in replay_events
            )
            after_snapshot = await request("GET", f"/sessions/{sid}")
            after_runs = await request("GET", f"/sessions/{sid}/runs")
            assert after_runs == before_runs
            assert after_snapshot["version"] == before_snapshot["version"]
            assert after_snapshot["messages"] == before_snapshot["messages"]
            assert raw_workspace(sid) == before
            with db_session() as db:
                user_rows = list(
                    db.scalars(
                        select(Message).where(
                            Message.session_id == sid, Message.role == "user"
                        )
                    )
                )
                matching = [
                    row
                    for row in user_rows
                    if (row.metadata_json or {}).get("guidance_request_id")
                    == transfer_answer_body["request_id"]
                ]
                assert len(matching) == 1
                assert (
                    matching[0].metadata_json["_guidance_response_context"]
                    == independent_evidence["response_context"]
                )
                assert db.get(LearningSession, sid).active_turn_id is None
            followup, _ = await act(
                sid,
                "reply",
                "我再次提交这道验证题的解题过程：\n" + simulated_transfer_answer,
            )
            after = raw_workspace(sid)
            evidence = after["evidence"][-1]
            assert (
                evidence["student_input_kind"] == "attempt"
                and evidence["is_attempt"] is True
            )
            assert evidence["transfer_response"] is True
            assert evidence["assisted_before_response"] is True
            assert evidence["independent_response"] is False
            assert after["evidence"][-2] == independent_evidence
            assert_public(followup["guidance"])
            return {
                "replayed_message_id": first["message"]["id"],
                "duplicate_evidence_count": 0,
                "duplicate_model_calls": 0,
                "submission_snapshot_persisted": True,
                "original_evidence_unchanged": True,
                "later_response_assisted": evidence["assisted_before_response"],
            }

        await check(
            "G09 MySQL恢复及JSON/SSE重放不重复证据，答后帮助影响下次作答",
            restore_and_replay_evidence,
        )

        async def help_before_response():
            fresh, _ = await act(
                sid,
                "transfer",
                "请重新给一道未出现的新情境供我独立作答，先不提供任何方法或提示。",
            )
            before_hint = raw_workspace(sid)
            assert before_hint["transfer"]["assisted"] is False
            await act(
                sid,
                "hint",
                "这道新题我还没有作答，请先给一个明确的局部提示；讲完先停顿，不要附加问题。",
            )
            after_hint = raw_workspace(sid)
            assert after_hint["transfer"]["assisted"] is True
            assert after_hint["evidence"][-1]["is_attempt"] is False
            assert after_hint["evidence"][-1]["independent_response"] is False
            answer = (
                "现在我提交正式解题过程：\n"
                + after_hint["transfer"]["reference_solution"]
            )
            result, _ = await act(sid, "reply", answer)
            after_answer = raw_workspace(sid)
            evidence = after_answer["evidence"][-1]
            context = evidence["response_context"]
            assert evidence["raw_input"] == answer
            assert (
                evidence["student_input_kind"] == "attempt"
                and evidence["is_attempt"] is True
            )
            assert evidence["assisted_before_response"] is True
            assert evidence["independent_response"] is False
            assert context["problem_id"] == before_hint["transfer"]["id"]
            assert context["workspace_version"] == after_hint["version"]
            assert context["assisted"] is True
            assert context["support_event_ids"], "提示应留下提交前可审计的帮助事件"
            assert_public(fresh["guidance"])
            assert_public(result["guidance"])
            return {
                "transfer_id": before_hint["transfer"]["id"],
                "assisted_before_response": evidence["assisted_before_response"],
                "independent_response": evidence["independent_response"],
                "support_event_count": len(context["support_event_ids"]),
                "simulation_method": "answer_copied_from_private_reference_for_backend_protocol_test_only",
            }

        await check(
            "G10 先提示后提交明确记录为辅助作答，保留提交前帮助回执",
            help_before_response,
        )

        async def audit():
            from sqlalchemy import func, select
            from app.infrastructure.persistence.database import db_session
            from app.domains.sessions.models import LearningSession
            from app.domains.students.models import StudentState

            with db_session() as db:
                assert db.get_bind().dialect.name == "mysql"
                for identifier in report["sessions"].values():
                    row = db.get(LearningSession, identifier)
                    assert row.student_id == report["student_id"]
                    workspace = row.pending_context["guided_teaching"]
                    assert workspace["analysis"]["reference_solution"]
                    assert workspace["evidence"] and not row.active_turn_id
                assert (
                    db.scalar(
                        select(func.count())
                        .select_from(StudentState)
                        .where(StudentState.student_id == report["student_id"])
                    )
                    == 0
                )
            runs = []
            public_sources = []
            for identifier in report["sessions"].values():
                run_response = await request("GET", f"/sessions/{identifier}/runs")
                runs.extend(
                    run_response.get("items", [])
                    if isinstance(run_response, dict)
                    else run_response
                )
                snapshot = await request("GET", f"/sessions/{identifier}")
                assert_public(snapshot)
                public_sources.extend(
                    (snapshot.get("guidance") or {}).get("sources", [])
                )
            selected = [run for run in runs if run["skill"] == "guided_explanation"]
            assert selected and all(run["agent"] == "exercise" for run in selected)
            traces = [trace for run in selected for trace in run.get("tool_calls", [])]
            completed = [
                trace for trace in traces if trace.get("status") == "completed"
            ]
            assert completed, "未获得真实已完成工具轨迹，不能声称完成检索闭环"
            assert any(
                trace["name"] == "read_teaching_resource" for trace in completed
            ), "没有实际读取教学资料，不能把只查目录视为来源闭环"
            assert public_sources, "未形成任何真实资料引用，不能声称来源已经接通"
            report["agent_runs"] = selected
            capture_runtime_audit(report)
            assert report["model_usage"]["failed_run_count"] == 0
            return {
                "tool_names": sorted({trace["name"] for trace in completed}),
                "source_count": len(public_sources),
                **report["model_usage"],
            }

        await check("G11 隐私与掌握状态不变，真实工具、耗时、Token和重试留痕", audit)

    report["complete"] = True
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    report["wall_time_seconds"] = round(time.perf_counter() - args.started, 3)
    save()
    print(
        json.dumps(
            {
                "checks_passed": len(report["checks"]),
                "model_usage": report["model_usage"],
                "report": str(args.report),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8008/api")
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "data/reports/guided_teaching_refinement_acceptance.json",
    )
    args = parser.parse_args()
    args.started = time.perf_counter()
    try:
        asyncio.run(main(args))
    except Exception as exc:
        print(f"FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
