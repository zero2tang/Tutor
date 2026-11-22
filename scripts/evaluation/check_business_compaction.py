"""真实普通消息与冻结待答题的分层记忆及压缩验收；输入明确为工程模拟。"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

import httpx
from request_protocol import request_body as native_request_body

_request_bindings = {}

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from business_compaction_audit import business_usage, public_probe_question, read_business_audit, source_digest
from check_memory_context import stress_thought

PROBLEM = "求解一元一次方程4(x-2)=12，并用代入原式的方法检查自己的计算。"
PREFERENCE = ("这是工程验收的模拟学生请求。我明确希望以后所有课程都先给提示，等我尝试后再展开完整解答。"
              "对当前这门代数课程，以后请先解释等式变形的理由，再使用简写。这两项是持续偏好，不是只针对本题。")


def render_dialogue(report):
    rows = ["业务分层记忆与上下文压缩真实后端验收", "",
            "输入为公开题干及脚本模拟思考；后端、数据库、模型提取和摘要均走实际配置。", ""]
    for turn in report["turns"]:
        rows.extend([f"{turn['index']}. {turn['label']} ({turn['session_id']})", "模拟学生：",
            turn["student_text"], "真实后端公开回复：", turn.get("teacher_text", "[尚未返回或失败]"), ""])
    rows.extend(["完成：" + str(report["complete"]), "失败：" + json.dumps(report.get("failure"), ensure_ascii=False)])
    return "\n".join(rows) + "\n"


async def main(args):
    os.environ["TUTOR_DATABASE"] = args.database
    student = "evaluation_" + uuid4().hex[:12]
    started = time.perf_counter()
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "base_url": args.base_url,
        "database": args.database, "student_id": student, "course_id": args.course_id,
        "backend_source_sha256": source_digest(), "script_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "helper_sha256": sha256(Path(__file__).with_name("business_compaction_audit.py").read_bytes()).hexdigest(),
        "shared_audit_helper_sha256": sha256(Path(__file__).with_name("memory_context_audit.py").read_bytes()).hexdigest(),
        "stress_generator_sha256": sha256(Path(__file__).with_name("check_memory_context.py").read_bytes()).hexdigest(),
        "data_origin": "disclosed_engineering_synthetic_student_and_long_thoughts",
        "execution": {"http_concurrency": 1, "http_retries": 0, "timeout_seconds": 240,
            "http_concurrency_scope": "within_this_script_only",
            "other_acceptance_driver_may_overlap": True,
            "database_writes_from_script": 0, "model_mocking": False,
            "background_scope": "background_memory_worker_can_overlap_HTTP; no_capacity_claim"},
        "native_compaction_event": "context_budget with status containing compressed",
        "validation_scope": "current_messages_and_real_practice_frozen_pending_question; guidance_budget_failure_preserved_separately",
        "not_tested": ["真实学生学习增益", "完整provider输入留存", "摘要事实召回人工标注", "容量与费用基准"],
        "sessions": {}, "turns": [], "requests": [], "checks": [], "complete": False}
    args.report.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        args.report.with_name(args.report.stem + "_dialogue.txt").write_text(render_dialogue(report), encoding="utf-8")

    async def audit():
        return await asyncio.to_thread(read_business_audit, student, list(report["sessions"].values()))

    async with httpx.AsyncClient(base_url=args.base_url.rstrip("/") + "/api", timeout=240, trust_env=False) as client:
        async def request(method, path, body=None):
            body = await native_request_body(client, method, path, body, _request_bindings)
            item = {"method": method, "path": path, "attempt": 1}
            begin = time.perf_counter()
            try:
                response = await client.request(method, path, json=body)
                item["status"] = response.status_code
                if response.status_code != 200:
                    raise RuntimeError(f"HTTP {response.status_code}: {response.text[:900]}")
                return response.json()
            except Exception as exc:
                item["error"] = type(exc).__name__
                raise
            finally:
                item["duration_ms"] = round((time.perf_counter() - begin) * 1000, 2)
                report["requests"].append(item)
                save()

        async def check(label, operation):
            begin = time.perf_counter()
            item = {"label": label}
            try:
                item.update(status="passed", details=await operation())
                print("PASS " + label, flush=True)
            except Exception as exc:
                item.update(status="failed", error=str(exc)[:1500])
                raise
            finally:
                item["duration_ms"] = round((time.perf_counter() - begin) * 1000, 2)
                report["checks"].append(item)
                save()

        async def create(name, goal):
            result = await request("POST", "/sessions", {"student_id": student, "course_id": args.course_id,
                "goal": goal, "concept_ids": report["concept_ids"]})
            report["sessions"][name] = result["id"]
            return result

        async def guidance(sid, action, text, problem=None, label=None):
            before = await request("GET", f"/sessions/{sid}")
            body = {"action": action, "text": text, "request_id": str(uuid4()), "expected_version": before["version"]}
            if problem:
                body["problem"] = {"stem": problem, "source_kind": "pasted"}
            workspace = before.get("guidance") or {}
            turn = {"index": len(report["turns"]) + 1, "label": label or "guidance/" + action,
                "session_id": sid, "request_id": body["request_id"], "student_text": text,
                "before": {"root_goal_id": before["session_goals"]["root_goal_id"],
                    "problem_id": workspace.get("problem", {}).get("id"),
                    "pending_question_id": workspace.get("pending_question_id"),
                    "pending_question_sha256": sha256(workspace.get("pending_question", "").encode()).hexdigest(),
                    "assisted": workspace.get("assisted", False), "help_level": workspace.get("help_level", 0)}}
            report["turns"].append(turn)
            result = await request("POST", f"/sessions/{sid}/guidance?stream=false", body)
            turn.update(teacher_text=result.get("message", {}).get("content", ""), run_id=result.get("run_id"))
            after = await request("GET", f"/sessions/{sid}")
            assert after["session_goals"]["root_goal_id"] == before["session_goals"]["root_goal_id"]
            state = after["guidance"]
            assert state["problem"]["stem"] == PROBLEM
            if workspace.get("problem"):
                assert state["problem"]["id"] == workspace["problem"]["id"]
            if workspace.get("assisted"):
                assert state["assisted"], "帮助状态出现回退"
            turn["after"] = {"pending_question_id": state.get("pending_question_id"),
                "assisted": state["assisted"], "help_level": state["help_level"],
                "goal_version": after["session_goals"]["version"]}
            save()
            return result, after

        try:
            courses = await request("GET", "/courses")
            items = courses.get("items", courses) if isinstance(courses, dict) else courses
            assert any(item["id"] == args.course_id for item in items), "验收课程未存在，请传入代数课程ID"
            catalog = await request("GET", "/concepts?course_id=" + args.course_id)
            concepts = catalog.get("items", [])
            candidates = [c for c in concepts if any(word in c.get("name", "") for word in ("方程", "分配律", "等式"))]
            assert candidates, "目录缺少本题相关知识点，不能只为通过而关联其它概念"
            report["concept_ids"] = [c["id"] for c in candidates[:2]]
            report["concept_names"] = [c["name"] for c in candidates[:2]]

            async def scoped_anchor():
                anchor = await create("anchor", "工程验收：记录明确持续的总体与本课程偏好")
                text = PREFERENCE + "请只确认这些持续偏好，暂不讲课、不出新题。"
                turn = {"index": len(report["turns"]) + 1, "label": "anchor/current-messages-explicit-preferences",
                        "session_id": anchor["id"], "student_text": text}
                report["turns"].append(turn)
                response = await request("POST", f"/sessions/{anchor['id']}/messages?stream=false",
                    {"content": text, "concept_ids": [], "request_id": str(uuid4()), "intent": "auto"})
                turn["teacher_text"] = response.get("message", {}).get("content", "")
                queued = await request("POST", f"/sessions/{anchor['id']}/memory/refresh?wait=true", {"close": False})
                report["anchor_refresh_job_id"] = queued.get("job_id")
                deadline = time.monotonic() + 240
                while True:
                    snapshot = await request("GET", f"/sessions/{anchor['id']}/memory")
                    available = [m for m in [snapshot.get("global_memory"), snapshot.get("course_memory"),
                        *snapshot.get("concept_memories", [])] if m and m.get("content") and m.get("status") == "active"]
                    if available:
                        report["anchor_scopes"] = [{key: m.get(key) for key in ("scope_key", "scope_kind", "version", "concept_id")}
                                                  for m in available]
                        return {"scope_count": len(available), "scopes": report["anchor_scopes"],
                                "real_model_extraction": True, "new_student": student}
                    if time.monotonic() > deadline:
                        raise TimeoutError("分层记忆未生成，不能用空scope回执通过压缩验收")
                    await asyncio.sleep(2)

            await check("BC01 独立模拟学生经真实HTTP形成可读取的分层记忆", scoped_anchor)

            async def compaction():
                stress = await create("stress", "工程压缩验收：保持已冻结的原题目标、当前待答和帮助条件。未正式提交答案前不要自动判分或换题。")
                assert stress["session_goals"]["root_goal_id"] != (await request("GET", f"/sessions/{report['sessions']['anchor']}"))["session_goals"]["root_goal_id"]
                problem = await asyncio.to_thread(public_probe_question, args.course_id)
                report["public_frozen_problem"] = problem
                await request("POST", f"/sessions/{stress['id']}/practice?stream=false",
                    {"question_id": problem["id"], "cognitive_type": "understanding"})
                initial = await request("GET", f"/sessions/{stress['id']}")
                assert initial["pending_question"]["id"] == problem["id"]
                initial_audit = await audit()
                initial_integrity = next(item for item in initial_audit["pending_integrity"] if item["session_id"] == stress["id"])
                report["pending_baseline"] = initial_integrity
                for index in range(args.max_turns):
                    before = await request("GET", f"/sessions/{stress['id']}")
                    long_thought = stress_thought(index).replace("4(x-2)=12", "当前已冻结的待答题")
                    long_thought = long_thought.replace("括号前面的4", "括号前面的系数").replace("两边同时除以4", "两边执行同一种运算")
                    text = ("这是工程压力验收的脚本模拟草稿，不是正式答案，请勿判分、勿出新题、勿替换当前待答题。\n"
                        + "当前公开原题：" + problem["stem"] + "\n" + long_thought
                        + "\n这些是我尚未完成的思考记录。请只简短确认收到和保留原题，不补充解法或提示，等待我正式提交。")
                    assert len(text) < 5000 and len(text) >= len(stress_thought(index))
                    turn = {"index": len(report["turns"]) + 1, "label": "messages/frozen-pending-stress-" + str(index),
                        "session_id": stress["id"], "student_text": text,
                        "before": {"root_goal_id": before["session_goals"]["root_goal_id"],
                                   "pending_question_id": before["pending_question"]["id"], "assisted": before["pending_assisted"]}}
                    report["turns"].append(turn)
                    result = await request("POST", f"/sessions/{stress['id']}/messages?stream=false",
                        {"content": text, "concept_ids": report["concept_ids"], "request_id": str(uuid4()), "intent": "auto"})
                    turn["teacher_text"] = result.get("message", {}).get("content", "")
                    after = await request("GET", f"/sessions/{stress['id']}")
                    assert after["session_goals"]["root_goal_id"] == stress["session_goals"]["root_goal_id"]
                    assert after["pending_question"] and after["pending_question"]["id"] == problem["id"]
                    assert after["pending_question"]["stem"] == problem["stem"]
                    if before["pending_assisted"]:
                        assert after["pending_assisted"], "帮助状态不能因压缩回退"
                    turn["after"] = {"pending_question_id": after["pending_question"]["id"],
                                     "assisted": after["pending_assisted"]}
                    snapshot = await audit()
                    compressed = [event for event in snapshot["events"] if event["event"] == "context_budget"
                        and event["session_id"] == stress["id"] and "compressed" in event["data"].get("status", "")]
                    if index + 1 >= args.min_turns and compressed:
                        break
                snapshot = await audit()
                compressed = [event["data"] for event in snapshot["events"] if event["event"] == "context_budget"
                    and event["session_id"] == stress["id"] and "compressed" in event["data"].get("status", "")]
                assert compressed and all(event["before_tokens"] > event["after_tokens"] for event in compressed)
                checkpoints = [row for row in snapshot["checkpoints"] if row["session_id"] == stress["id"]]
                assert checkpoints and all(row["source_coverage_valid"] for row in checkpoints)
                messages = {item["content_sha256"] for item in snapshot["messages"]
                            if item["session_id"] == stress["id"] and item["role"] == "user"}
                stress_turns = [turn for turn in report["turns"] if turn["session_id"] == stress["id"]]
                assert all(sha256(turn["student_text"].encode()).hexdigest() in messages for turn in stress_turns)
                final_pending = next(item for item in snapshot["pending_integrity"] if item["session_id"] == stress["id"])
                assert final_pending["contract_sha256"] == initial_integrity["contract_sha256"]
                assert final_pending["pending_question_id"] == initial_integrity["pending_question_id"]
                budgets = [event["data"] for event in snapshot["events"] if event["event"] == "context_budget"
                    and event["session_id"] == stress["id"] and not event["data"].get("status", "").startswith("blocked")]
                assert all(event["after_tokens"] <= event["input_budget"] for event in budgets)
                compressed_runs = {event["data"].get("run_id") for event in snapshot["events"]
                    if event["event"] == "context_budget" and event["session_id"] == stress["id"]
                    and "compressed" in event["data"].get("status", "")}
                receipts = [event["data"] for event in snapshot["events"] if event["event"] == "memory_context_loaded"
                    and event["data"].get("run_id") in compressed_runs]
                assert receipts and any(item.get("scope_receipts") for item in receipts), "压缩运行缺少非空业务记忆加载回执"
                assert all(item.get("memory_schema") == "business-memory-v2" for item in receipts)
                assert all(not item.get("session_experience_ids") for item in receipts), "日常教学仍在默认注入整份经历"
                return {"rounds": len(stress_turns), "compressed_events": compressed,
                    "checkpoint_count": len(checkpoints), "original_messages_preserved": True,
                    "frozen_contract_unchanged": True, "pending_help_state": final_pending,
                    "native_event": "context_budget", "normalized_check": "context_compacted",
                    "scoped_memory_receipts": receipts, "compressed_run_ids": sorted(compressed_runs),
                    "same_session_root_goal": True,
                    "provider_full_payload_capture": False}

            await check("BC02 实际模型压缩后原题目标、原话、待答、帮助条件及分层回执保留", compaction)

            async def settled_jobs():
                deadline = time.monotonic() + 240
                while True:
                    snapshot = await audit()
                    jobs = [*snapshot["jobs"], *snapshot["scope_jobs"]]
                    waiting = [j for j in jobs if j["status"] in {"pending", "running", "retry"}]
                    if not waiting:
                        assert not [j for j in jobs if j["status"] == "failed"], "本轮后台任务存在失败终态"
                        return {"owned_jobs": len(jobs), "all_terminal": True}
                    if time.monotonic() > deadline:
                        raise TimeoutError("本学生后台任务未收敛")
                    await asyncio.sleep(2)

            await check("BC03 本轮叶子及各作用域任务收敛", settled_jobs)
            report["complete"] = True
        except Exception as exc:
            report["failure"] = {"type": type(exc).__name__, "message": str(exc)[:2500]}
            print("FAIL " + type(exc).__name__ + ": " + str(exc)[:900], flush=True)
        finally:
            try:
                report["audit"] = await audit()
                report["model_usage"] = business_usage(report["audit"])
                report["backend_source_sha256_after"] = source_digest()
                report["source_unchanged_during_acceptance"] = report["backend_source_sha256_after"] == report["backend_source_sha256"]
                if not report["source_unchanged_during_acceptance"]:
                    report["complete"] = False
            except Exception as exc:
                report["audit_failure"] = {"type": type(exc).__name__, "message": str(exc)[:800]}
                report["complete"] = False
            report["ended_at"] = datetime.now(timezone.utc).isoformat()
            report["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
            save()
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8012")
    parser.add_argument("--database", default="stem_tutor_business_memory_acceptance")
    parser.add_argument("--course-id", default="junior_algebra")
    parser.add_argument("--min-turns", type=int, default=4)
    parser.add_argument("--max-turns", type=int, default=7)
    parser.add_argument("--report", type=Path, default=ROOT / "data/reports/business_compaction_acceptance.json")
    args = parser.parse_args()
    if not 3 <= args.min_turns <= args.max_turns <= 10:
        parser.error("真实长文本验收需满足3≤min-turns≤max-turns≤10")
    if args.database != "stem_tutor_business_memory_acceptance":
        parser.error("此脚本只允许本轮专属业务记忆隔离验收库")
    if not re.fullmatch(r"http://(?:127\.0\.0\.1|localhost):8012", args.base_url.rstrip("/")):
        parser.error("此脚本只允许本机8012隔离验收服务")
    raise SystemExit(asyncio.run(main(args)))
