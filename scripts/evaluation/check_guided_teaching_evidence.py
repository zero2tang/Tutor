"""真实讲题证据验收：隔离学生模型仅依据公开题干和师生对话作答。"""

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

from check_guided_teaching import (
    assert_public,
    capture_runtime_audit,
    frames,
    source_digest,
)
from public_learner import PublicLearner, canonical_hash

ORIGINAL_PROBLEM = (
    "【自编应用题】学校制作一批相同的海报，每张印刷费8元，另付一次性的制版费24元。"
    "这批海报共花168元。学校制作了多少张海报？请写出数量关系、求解过程并检验。"
)


def evidence_snapshot(session_id: str) -> dict:
    """只抽取验收需要的证据和帮助记录；不导出、传递内部参考解。"""
    from app.infrastructure.persistence.database import db_session
    from app.domains.sessions.models import LearningSession

    with db_session() as db:
        row = db.get(LearningSession, session_id)
        from app.domains.guidance.history import hydrate
        state = hydrate(db,row.pending_context["guided_teaching"])
        transfer = state.get("transfer")
        return deepcopy(
            {
                "id": state["id"],
                "version": state["version"],
                "evidence": state["evidence"],
                "support_history": state["support_history"],
                "transfer": (
                    {
                        key: transfer.get(key)
                        for key in (
                            "id",
                            "stem",
                            "assisted",
                            "help_level",
                            "response_count",
                        )
                    }
                    if transfer
                    else None
                ),
            }
        )


def render_dialogue(report: dict) -> str:
    rows = [
        "真实后端讲题完整对话（隔离模型模拟学生）",
        "",
        "这不是自然班级实验。初始误解、请求提示和教学方式请求由脚本编排；三次正式解题由隔离learner模型生成。",
        "learner仅收到下列公开题干与此前公开对话，没有系统提示、工具、数据库、工作区、参考解或隐藏解题约束。",
        "learner调用的原始messages、请求体SHA-256、输出、Token与请求次数保存在JSON报告中。",
        "教师文字保留原文。独立性只描述本题提交前的帮助条件，不证明真实学生掌握或思路来源。",
        "本轮SSE采用校验后正文发布；进度事件实时传输，正文不再声称上游即时增量。",
        "",
        "原题：" + ORIGINAL_PROBLEM,
        "",
    ]
    for turn in report.get("turns", []):
        rows.extend(
            [
                f"第{turn['index']}轮 · {turn['label']}",
                f"学生（{turn['student_origin']}）：",
                turn["student_text"],
                "",
                "教师（真实后端原文）：",
                turn.get("teacher_text", "[本轮失败，未发布教师正文]"),
                "",
            ]
        )
        evidence = turn.get("evidence")
        if evidence:
            rows.extend(
                [
                    "本轮后端证据："
                    + json.dumps(
                        {
                            key: evidence.get(key)
                            for key in (
                                "student_input_kind",
                                "is_attempt",
                                "assisted_before_response",
                                "independent_response",
                                "transfer_response",
                            )
                        },
                        ensure_ascii=False,
                    ),
                    "提交前帮助事件："
                    + json.dumps(
                        evidence.get("response_context", {}).get(
                            "support_event_ids", []
                        ),
                        ensure_ascii=False,
                    ),
                    "公开证据说明："
                    + json.dumps(turn.get("evidence_feedback"), ensure_ascii=False),
                    "",
                ]
            )
    rows.extend(
        [
            "验收状态：" + ("通过" if report.get("complete") else "未完成或失败"),
            "learner用量："
            + json.dumps(report.get("learner_usage", {}), ensure_ascii=False),
            "教师后端用量："
            + json.dumps(report.get("model_usage", {}), ensure_ascii=False),
            "",
        ]
    )
    return "\n".join(rows)


async def main(args):
    learner = PublicLearner()
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "backend_source_sha256": source_digest(),
        "acceptance_script_sha256": hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest(),
        "learner_helper_sha256": hashlib.sha256(
            Path(__file__).with_name("public_learner.py").read_bytes()
        ).hexdigest(),
        "student_id": "evaluation_" + uuid4().hex[:12],
        "sessions": {},
        "requests": [],
        "turns": [],
        "checks": [],
        "learner_calls": learner.calls,
        "data_origin": {
            "original_problem": "self_authored_public_problem",
            "transfer_problem": "teacher_model_generated_and_publicly_published",
            "initial_misconception_and_teaching_requests": "script_authored_and_disclosed",
            "formal_answers": "isolated_real_learner_model_public_context_only",
        },
        "execution": {
            "concurrency": 1,
            "http_retry_policy": "none",
            "learner_system_messages": 0,
            "learner_tools": 0,
            "streaming_contract": "progress_real_time_text_validated_publication",
            "independent_publication_audit": (
                "teacher_message_and_citations_plus_guidance_focus_analysis_summary_sources_transfer_target"
            ),
        },
        "not_tested": [
            "真实学生学习增益",
            "长期保持",
            "通用学科解题正确率",
            "掌握概率校准",
        ],
        "complete": False,
        "failure_count": 0,
        "transport_failure_count": 0,
    }
    dialogue: list[dict] = []
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.dialogue.parent.mkdir(parents=True, exist_ok=True)

    def save():
        report["learner_usage"] = learner.usage()
        report["wall_time_seconds"] = round(time.perf_counter() - args.started, 3)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        args.dialogue.write_text(render_dialogue(report), encoding="utf-8")

    def checked(label, details):
        report["checks"].append(
            {"label": label, "status": "passed", "details": details}
        )
        print("PASS " + label, flush=True)
        save()

    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"), timeout=args.timeout, trust_env=False
    ) as http:

        async def request(method, path, body=None, *, sse=False):
            body = await native_request_body(http, method, path, body, _request_bindings)
            started = time.perf_counter()
            metric = {"method": method, "path": path}
            report["requests"].append(metric)
            try:
                if sse:
                    chunks = []
                    async with http.stream(method, path, json=body) as response:
                        metric["status"] = response.status_code
                        async for chunk in response.aiter_text():
                            if chunk and "first_bytes_ms" not in metric:
                                metric["first_bytes_ms"] = round(
                                    (time.perf_counter() - started) * 1000, 3
                                )
                            chunks.append(chunk)
                    text = "".join(chunks)
                else:
                    response = await http.request(method, path, json=body)
                    metric["status"] = response.status_code
                    text = response.text
                assert (
                    response.status_code == 200
                ), f"HTTP {response.status_code}: {text[:1000]}"
                if not sse:
                    return response.json()
                assert response.headers.get("content-type", "").startswith(
                    "text/event-stream"
                )
                events = frames(text)
                assert events and events[-1][0] == "done", events[-3:]
                assert not any(name == "error" for name, _ in events), events
                metric["sse_event_names"] = [name for name, _ in events]
                metric["text_delta_count"] = sum(
                    name == "text_delta" for name, _ in events
                )
                return events
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                report["transport_failure_count"] += 1
                metric["transport_error"] = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                metric["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)

        sid = None
        try:
            session = await request(
                "POST",
                "/sessions",
                {
                    "course_id": "junior_algebra",
                    "student_id": report["student_id"],
                    "goal": "真实公开对话验证建模帮助及独立作答证据",
                    "concept_ids": [],
                },
            )
            sid = session["id"]
            report["sessions"]["guided_evidence"] = sid

            async def act(
                label, action, student_text, *, origin="脚本编排", start=False
            ):
                snapshot = await request("GET", f"/sessions/{sid}")
                body = {
                    "action": action,
                    "text": student_text,
                    "request_id": str(uuid4()),
                    "expected_version": (snapshot.get("guidance") or {}).get(
                        "version", 0
                    ),
                }
                if start:
                    body["problem"] = {
                        "stem": ORIGINAL_PROBLEM,
                        "source_kind": "pasted",
                    }
                turn = {
                    "index": len(report["turns"]) + 1,
                    "label": label,
                    "student_origin": origin,
                    "student_text": (
                        (ORIGINAL_PROBLEM + "\n\n" + student_text)
                        if start
                        else student_text
                    ),
                    "request": body,
                }
                report["turns"].append(turn)
                if not start:
                    dialogue.append({"speaker": "student", "content": student_text})
                events = await request(
                    "POST", f"/sessions/{sid}/guidance?stream=true", body, sse=True
                )
                turn["sse_events"] = [
                    {"event": name, "data": data} for name, data in events
                ]
                message = next(
                    data
                    for name, data in reversed(events)
                    if name == "message" and data.get("role") == "assistant"
                )
                guidance = next(
                    data for name, data in reversed(events) if name == "guidance"
                )
                assert_public(guidance)
                turn.update(
                    teacher_text=message["content"],
                    public_message=message,
                    guidance=guidance,
                )
                state = evidence_snapshot(sid)
                evidence = next(
                    item
                    for item in reversed(state["evidence"])
                    if item["request_id"] == body["request_id"]
                )
                turn["evidence"] = evidence
                turn["new_support_events"] = [
                    item
                    for item in state["support_history"]
                    if item["request_id"] == body["request_id"]
                ]
                # 公共元数据是服务端根据已冻结证据生成的，不采用教师模型的独立性判断。
                metadata = message.get("metadata") or {}
                feedback = metadata.get("evidence_feedback") or guidance.get(
                    "evidence_feedback"
                )
                turn["evidence_feedback"] = feedback
                assert (
                    feedback is not None
                ), f"未找到evidence_feedback: {message.keys()}, {metadata.keys()}"
                expected = (
                    "not_attempt"
                    if not evidence["is_attempt"]
                    else (
                        "assisted"
                        if evidence["assisted_before_response"]
                        else (
                            "independent_recorded_conditions"
                            if evidence["independent_response"]
                            else "no_recorded_help"
                        )
                    )
                )
                assert feedback["condition"] == expected, (feedback, evidence)
                assert feedback["is_attempt"] == evidence["is_attempt"]
                assert (
                    feedback["independent_response"] == evidence["independent_response"]
                )
                assert (
                    feedback["assisted_before_response"]
                    == evidence["assisted_before_response"]
                )
                if feedback["statement"]:
                    assert feedback["statement"] in message["content"]
                assert metadata["delivery_mode"] == "validated_publication"
                # 已发布的任何增量都必须来自本轮最终通过校验的正文；不能先泄漏错误宣称再重置。
                delta_text = "".join(
                    data.get("delta", "")
                    for name, data in events
                    if name == "text_delta"
                )
                assert all(
                    data.get("delivery_mode") == "validated_publication"
                    for name, data in events
                    if name == "text_delta"
                )
                assert not any(
                    name == "text_reset" for name, _ in events
                ), "校验后发布不应出现先泄漏再撤回"
                if delta_text:
                    assert (
                        delta_text in message["content"]
                    ), "存在最终正文以外的公开增量"
                dialogue.append({"speaker": "teacher", "content": message["content"]})
                save()
                print("TURN " + label, flush=True)
                return turn, state

            first, _ = await act(
                "原题发布",
                "start",
                "请先只呈现题干并中性邀请我尝试，不给方法、关系式或答案。之后按我的实际想法逐步引导，每轮尽量简洁。",
                start=True,
            )
            checked(
                "E01 真实HTTP与SSE进入讲题，公开状态不泄露私有分析",
                {"session_id": sid, "first_teacher": first["teacher_text"]},
            )

            await act(
                "主动尝试暴露误解",
                "reply",
                "我先算168÷8=21，所以觉得做了21张。但题目还有24元制版费，我不知道这笔钱该放在哪里。先帮我定位这个想法的问题，不直接给最终张数。",
            )
            hint, hint_state = await act(
                "请求并获得建模结构提示",
                "hint",
                "我还是卡在怎么把题意写成关系式。请明确给一个建模结构提示，不给最终张数；说完先停顿，不附加问题，尽量100字以内。",
            )
            assert hint["new_support_events"], "建模结构未留下帮助事件"
            assert (
                max(item["help_level"] for item in hint["new_support_events"]) >= 2
            ), hint["new_support_events"]
            assert hint["guidance"]["response_expectation"] == "none"
            checked(
                "E02 建模结构提示计入帮助，解释自然停顿",
                {
                    "support_events": hint["new_support_events"],
                    "response_expectation": "none",
                },
            )

            original_answer = await learner.answer(ORIGINAL_PROBLEM, dialogue)
            assisted, _ = await act(
                "隔离学生自行推导原题",
                "reply",
                original_answer
                + "\n\n请核对我的作答，简短反馈后先停顿，不追加问题或新题。",
                origin="隔离learner生成解题正文；反馈方式请求由脚本追加",
            )
            assert assisted["evidence"]["is_attempt"] is True
            assert assisted["evidence"]["assisted_before_response"] is True
            assert assisted["evidence"]["independent_response"] is False
            assert assisted["guidance"]["response_expectation"] == "none"
            checked(
                "E03 公开提示后的模型作答记录为辅助，教师说明一致",
                {
                    "evidence": assisted["evidence"],
                    "evidence_feedback": assisted["evidence_feedback"],
                },
            )

            transfer, new_state = await act(
                "发布无提示近迁移题",
                "transfer",
                "请给一道数字与情境都不同的近迁移题，让我先独立尝试。只发布完整题干和中性作答邀请，暂不提供方法、建模结构或答案。",
            )
            assert new_state["transfer"] and new_state["transfer"]["assisted"] is False
            assert transfer["new_support_events"] == [], transfer["new_support_events"]
            assert transfer["guidance"]["focus"] == ""
            assert transfer["guidance"]["analysis_summary"] == ""
            assert transfer["guidance"]["sources"] == []
            assert transfer["guidance"]["transfer"]["target"] == "新题作答"
            assert transfer["public_message"]["citations"] == []
            checked(
                "E04 新题所有公开字段只发布题干与中性邀请，不泄漏建模结构",
                {
                    "transfer": new_state["transfer"],
                    "teacher_text": transfer["teacher_text"],
                    "public_projection": {
                        "focus": transfer["guidance"]["focus"],
                        "analysis_summary": transfer["guidance"]["analysis_summary"],
                        "sources": transfer["guidance"]["sources"],
                        "transfer_target": transfer["guidance"]["transfer"]["target"],
                        "message_citations": transfer["public_message"]["citations"],
                    },
                },
            )

            transfer_answer = await learner.answer(ORIGINAL_PROBLEM, dialogue)
            independent, after_explain = await act(
                "先独立提交后教师补讲",
                "reply",
                transfer_answer
                + "\n\n这是我已提交的解题过程。请核对后简洁完整补讲列式依据和求解过程，讲完自然停顿，不附加问题或新题。",
                origin="隔离learner生成解题正文；补讲请求由脚本追加",
            )
            independent_evidence = deepcopy(independent["evidence"])
            assert independent_evidence["is_attempt"] is True
            assert independent_evidence["assisted_before_response"] is False
            assert independent_evidence["independent_response"] is True
            assert (
                independent_evidence["response_context"]["problem_id"]
                == new_state["transfer"]["id"]
            )
            assert after_explain["transfer"]["id"] == new_state["transfer"]["id"]
            assert after_explain["transfer"]["assisted"] is True
            assert independent["guidance"]["response_expectation"] == "none"
            checked(
                "E05 独立提交的证据不被答后补讲改写，教师说明一致",
                {
                    "evidence": independent_evidence,
                    "evidence_feedback": independent["evidence_feedback"],
                },
            )

            restated, restated_state = await act(
                "请求同题重新作答",
                "reply",
                "我想再完整写一次刚才的验证题。请只重述同一道题并邀请我作答，不补充方法，也不要换题。",
            )
            transfer_id = new_state["transfer"]["id"]
            assert (
                restated_state["transfer"]["id"] == transfer_id
            ), "同题重述擅自替换了验证题"
            assert restated["guidance"]["transfer"]["id"] == transfer_id
            assert restated["evidence"]["response_context"]["problem_id"] == transfer_id
            assert restated_state["transfer"]["assisted"] is True
            repeated_answer = await learner.answer(ORIGINAL_PROBLEM, dialogue)
            repeated, final_state = await act(
                "补讲后同题再答",
                "reply",
                repeated_answer + "\n\n请简短核对后自然停顿，不追加问题。",
                origin="隔离learner再次生成解题正文；反馈方式请求由脚本追加",
            )
            assert repeated["evidence"]["is_attempt"] is True
            assert repeated["evidence"]["response_context"]["problem_id"] == transfer_id
            assert final_state["transfer"]["id"] == transfer_id
            assert repeated["evidence"]["assisted_before_response"] is True
            assert repeated["evidence"]["independent_response"] is False
            assert (
                next(
                    item
                    for item in final_state["evidence"]
                    if item["id"] == independent_evidence["id"]
                )
                == independent_evidence
            )
            checked(
                "E06 同题重述与再答保持题目身份，辅助记录不改写之前独立证据",
                {
                    "transfer_id": transfer_id,
                    "restated_transfer_id": restated_state["transfer"]["id"],
                    "repeated_response_problem_id": repeated["evidence"][
                        "response_context"
                    ]["problem_id"],
                    "evidence": repeated["evidence"],
                    "evidence_feedback": repeated["evidence_feedback"],
                },
            )

            assert len(learner.calls) == 3
            for call in learner.calls:
                assert canonical_hash(call["payload"]) == call["payload_sha256"]
                assert "tools" not in call["payload"]
                assert all(
                    item["role"] != "system" for item in call["payload"]["messages"]
                )
            checked(
                "E07 三次正式作答均来自公开上下文隔离learner，无参考解复制",
                {
                    "calls": len(learner.calls),
                    "payload_sha256": [
                        item["payload_sha256"] for item in learner.calls
                    ],
                    "usage": learner.usage(),
                },
            )

            capture_runtime_audit(report)
            assert report["database_dialect"] == "mysql"
            assert report["model_usage"]["failed_run_count"] == 0
            checked(
                "E08 MySQL与真实教师模型完成，无失败运行，Token与重试留痕",
                report["model_usage"],
            )
            report["complete"] = True
            report["completed_at"] = datetime.now(timezone.utc).isoformat()
        except Exception as exc:
            report["failure_count"] += 1
            report["failure"] = f"{type(exc).__name__}: {exc}"
            if sid:
                try:
                    capture_runtime_audit(report)
                except Exception as audit_error:
                    report["audit_error"] = (
                        f"{type(audit_error).__name__}: {audit_error}"
                    )
            raise
        finally:
            await learner.close()
            save()
    print(
        json.dumps(
            {
                "complete": report["complete"],
                "checks_passed": len(report["checks"]),
                "teacher_usage": report.get("model_usage"),
                "learner_usage": report["learner_usage"],
                "report": str(args.report),
                "dialogue": str(args.dialogue),
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
        default=ROOT / "data/reports/guided_teaching_evidence_acceptance.json",
    )
    parser.add_argument(
        "--dialogue",
        type=Path,
        default=ROOT / "data/reports/guided_teaching_evidence_dialogue.txt",
    )
    args = parser.parse_args()
    args.started = time.perf_counter()
    asyncio.run(main(args))
