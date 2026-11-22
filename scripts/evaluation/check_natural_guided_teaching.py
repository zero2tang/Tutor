"""不附加用户教学元指令，观察真实网页后端入口的默认讲题行为。"""

from __future__ import annotations

import argparse
import asyncio
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

from check_guided_teaching import assert_public, frames, source_digest
from check_guided_teaching_evidence import evidence_snapshot
from natural_teaching_audit import (
    assert_natural_student_text,
    audit_published_turn,
    capture_all_run_usage,
)
from public_learner import PublicLearner, canonical_hash

SCENARIOS = (
    {
        "id": "poster",
        "stem": "学校制作一批相同的海报，每张印刷费8元，另付一次性的制版费24元。这批海报共花168元。学校制作了多少张海报？",
        "replies": (
            "我算168÷8=21，所以是21张吗？",
            "我还是不会列式。",
            None,
            "为什么要先减24？",
            "我还是有点乱，能完整讲一遍吗？",
            "懂了。",
            "先到这里吧。",
        ),
    },
    {
        "id": "brackets",
        "stem": "这题怎么做：3(2x-1)-2(x+4)=9。",
        "replies": (
            "我不会去括号。",
            "为什么-2(x+4)展开后是-2x-8，不是-2x+8？",
            "我想看完整的解题过程。",
        ),
    },
    {
        "id": "number",
        "stem": "一个数的3倍减去7，等于这个数加上13，这个数是多少？",
        "replies": ("我不会。", "再给我一道类似的题，我想试试。"),
    },
    {
        "id": "plain_chat",
        "stem": "学校制作一批相同的海报，每张印刷费8元，另付一次性的制版费24元。这批海报共花168元。学校制作了多少张海报？这题怎么做？",
        "entry": "ordinary_chat",
        "replies": (),
    },
)


def render_dialogue(report):
    rows = [
        "无用户教学元指令的真实讲题观察",
        "",
        "前三个情境首轮与网页粘贴题干一致：start.text为空；后续全部走普通/messages。另设一个未开启讲题工作区的普通聊天入口对照。",
        "普通困惑和错误由脚本设定；learner回合只见公开题干与公开对话，解题正文原样提交，不追加反馈要求。",
        "脚本只检查协议和证据；默认启发式教学质量由另列的人工审阅判断，不把HTTP成功等同于教学合格。",
        "",
    ]
    for scenario in report["scenarios"]:
        rows.extend([f"情境：{scenario['id']}", "题干：" + scenario["stem"], ""])
        for turn in scenario["turns"]:
            rows.extend(
                [
                    f"第{turn['index']}轮 学生（{turn['student_origin']}）：",
                    turn["student_text"],
                    "",
                    "教师：",
                    turn.get("teacher_text", "[未发布正文]"),
                    "",
                    "客观记录："
                    + json.dumps(turn.get("observations", {}), ensure_ascii=False),
                    "",
                ]
            )
            if turn.get("error"):
                rows.extend(["失败：" + turn["error"], ""])
    rows.extend(
        [
            "技术检查：" + json.dumps(report["checks"], ensure_ascii=False),
            "教学审阅：" + json.dumps(report["pedagogy_review"], ensure_ascii=False),
            "教师用量："
            + json.dumps(report.get("model_usage", {}), ensure_ascii=False),
            "学生用量："
            + json.dumps(report.get("learner_usage", {}), ensure_ascii=False),
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
        "audit_helper_sha256": hashlib.sha256(
            Path(__file__).with_name("natural_teaching_audit.py").read_bytes()
        ).hexdigest(),
        "learner_helper_sha256": hashlib.sha256(
            Path(__file__).with_name("public_learner.py").read_bytes()
        ).hexdigest(),
        "sessions": {},
        "scenarios": [],
        "requests": [],
        "checks": [],
        "learner_calls": learner.calls,
        "execution": {
            "concurrency": 1,
            "start_text": "",
            "continuation_endpoint": "/sessions/{id}/messages",
            "continuation_intent": "auto",
            "special_action_buttons_used": False,
            "fresh_student_per_scenario": True,
            "session_goal": "学习一元一次方程",
            "learner_output_submitted_verbatim": True,
            "teacher_extra_test_instructions": False,
        },
        "pedagogy_review": {
            "status": "pending_manual_review",
            "rubric": [
                "三个首次提问：是否留下可回答的切入点，未默认公布整题答案；不能只看是否有问号。",
                "错误或不会：是否回应实际障碍并增加适量支架，不机械重复问题。",
                "原因追问：是否直接解释因果和适用依据，而非只反问。",
                "完整讲解请求：是否连贯给出依据、步骤和结论，不强制继续练习。",
                "懂了和结束：不冒充独立掌握证据，尊重停止意愿。",
                "自然语言请求类似题：是否能经普通聊天入口完成，不依赖隐藏按钮。",
            ],
            "findings": [],
        },
        "not_tested": [
            "真实学生学习效果",
            "长期保持",
            "通用学科教学质量",
            "统计稳定性",
        ],
        "failure_count": 0,
        "transport_failure_count": 0,
        "complete": False,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)

    def save():
        report["learner_usage"] = learner.usage()
        report["wall_time_seconds"] = round(time.perf_counter() - args.started, 3)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        args.dialogue.write_text(render_dialogue(report), encoding="utf-8")

    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"), timeout=args.timeout, trust_env=False
    ) as http:

        async def request(path, body):
            body = await native_request_body(http, "POST", path, body, _request_bindings)
            metric = {"path": path, "request_body": body}
            report["requests"].append(metric)
            started = time.perf_counter()
            try:
                response = await http.post(path, json=body)
                metric["status"] = response.status_code
                assert (
                    response.status_code == 200
                ), f"HTTP {response.status_code}: {response.text[:1000]}"
                if "stream=true" not in path:
                    return response.json()
                events = frames(response.text)
                assert events and events[-1][0] == "done", "SSE缺少done"
                return events
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                report["transport_failure_count"] += 1
                metric["transport_error"] = type(exc).__name__
                raise
            finally:
                metric["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)

        try:
            for config in SCENARIOS:
                scenario = {
                    "id": config["id"],
                    "stem": config["stem"],
                    "student_id": "evaluation_" + uuid4().hex[:12],
                    "turns": [],
                }
                report["scenarios"].append(scenario)
                session = await request(
                    "/sessions",
                    {
                        "course_id": "junior_algebra",
                        "student_id": scenario["student_id"],
                        "goal": "学习一元一次方程",
                        "concept_ids": [],
                    },
                )
                sid = session["id"]
                report["sessions"][config["id"]] = sid
                dialogue = []

                async def act(text, *, start=False, origin="脚本设定的普通学生话语"):
                    assert_natural_student_text(text)
                    key = str(uuid4())
                    plain_chat = start and config.get("entry") == "ordinary_chat"
                    if start and not plain_chat:
                        body = {
                            "action": "start",
                            "text": "",
                            "problem": {"stem": text, "source_kind": "pasted"},
                            "request_id": key,
                            "expected_version": 0,
                        }
                        path = f"/sessions/{sid}/guidance?stream=true"
                    else:
                        body = {"content": text, "request_id": key}
                        path = f"/sessions/{sid}/messages?stream=true"
                        dialogue.append({"speaker": "student", "content": text})
                    turn = {
                        "index": len(scenario["turns"]) + 1,
                        "student_text": text,
                        "student_origin": origin,
                        "request": body,
                        "path": path,
                    }
                    scenario["turns"].append(turn)
                    try:
                        events = await request(path, body)
                        turn["sse_events"] = [
                            {"event": name, "data": data} for name, data in events
                        ]
                        errors = [data for name, data in events if name == "error"]
                        assert not errors, json.dumps(errors, ensure_ascii=False)
                        message = next(
                            data
                            for name, data in reversed(events)
                            if name == "message" and data.get("role") == "assistant"
                        )
                        if plain_chat:
                            # 普通聊天未开启讲题工作区，不能强行把另一入口的结果当成该skill的证据。
                            snapshot = (await http.get(f"/sessions/{sid}")).json()
                            turn.update(
                                teacher_text=message["content"],
                                public_message=message,
                                observations={
                                    "entry": "ordinary_chat_without_guidance",
                                    "guidance_active": bool(snapshot.get("guidance")),
                                    "tutoring_active": bool(snapshot.get("tutoring")),
                                    "event_names": [name for name, _ in events],
                                },
                                status="completed",
                            )
                            report["checks"].append(
                                {"label": "普通聊天入口对照已留痕", "status": "passed"}
                            )
                            print("DONE plain_chat:1", flush=True)
                            return True
                        guidance = next(
                            data
                            for name, data in reversed(events)
                            if name == "guidance"
                        )
                        assert_public(guidance)
                        state = evidence_snapshot(sid)
                        evidence = next(
                            item
                            for item in reversed(state["evidence"])
                            if item["request_id"] == key
                        )
                        turn.update(
                            teacher_text=message["content"],
                            public_message=message,
                            guidance=guidance,
                            evidence=evidence,
                            evidence_feedback=message["metadata"]["evidence_feedback"],
                            new_support_events=[
                                item
                                for item in state["support_history"]
                                if item["request_id"] == key
                            ],
                        )
                        turn["observations"] = audit_published_turn(turn)
                        turn["status"] = "completed"
                        dialogue.append(
                            {"speaker": "teacher", "content": message["content"]}
                        )
                        report["checks"].append(
                            {
                                "label": f"{config['id']}:{turn['index']}公开流和证据一致",
                                "status": "passed",
                            }
                        )
                        print(
                            f"DONE {config['id']}:{turn['index']} action={evidence['teaching_action']} expectation={guidance['response_expectation']}",
                            flush=True,
                        )
                        return True
                    except Exception as exc:
                        turn.update(
                            status="failed", error=f"{type(exc).__name__}: {exc}"
                        )
                        report["failure_count"] += 1
                        report["checks"].append(
                            {
                                "label": f"{config['id']}:{turn['index']}",
                                "status": "failed",
                                "error": turn["error"],
                            }
                        )
                        print(
                            f"FAIL {config['id']}:{turn['index']} {turn['error']}",
                            flush=True,
                        )
                        return False
                    finally:
                        save()

                if not await act(
                    config["stem"],
                    start=True,
                    origin=(
                        "普通聊天直接提问"
                        if config.get("entry") == "ordinary_chat"
                        else "仅粘贴题干"
                    ),
                ):
                    continue
                for text in config["replies"]:
                    if text is None:
                        text = await learner.answer(config["stem"], dialogue)
                        origin = "隔离学生模型原样输出，无追加请求"
                        assert text == learner.calls[-1]["output"]
                        assert (
                            canonical_hash(learner.calls[-1]["payload"])
                            == learner.calls[-1]["payload_sha256"]
                        )
                    else:
                        origin = "脚本设定的普通学生话语"
                    if not await act(text, origin=origin):
                        break
            report["complete"] = report["failure_count"] == 0
        except Exception as exc:
            report["fatal_error"] = f"{type(exc).__name__}: {exc}"
            report["failure_count"] += 1
            raise
        finally:
            capture_all_run_usage(report)
            report["completed_at"] = datetime.now(timezone.utc).isoformat()
            save()
            await learner.close()
    print(
        json.dumps(
            {
                "technical_complete": report["complete"],
                "failures": report["failure_count"],
                "wall_time_seconds": report["wall_time_seconds"],
                "model_usage": report.get("model_usage"),
                "learner_usage": report["learner_usage"],
                "pedagogy_status": report["pedagogy_review"]["status"],
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
        default=ROOT / "data/reports/natural_guided_teaching_acceptance.json",
    )
    parser.add_argument(
        "--dialogue",
        type=Path,
        default=ROOT / "data/reports/natural_guided_teaching_dialogue.txt",
    )
    args = parser.parse_args()
    args.started = time.perf_counter()
    asyncio.run(main(args))
