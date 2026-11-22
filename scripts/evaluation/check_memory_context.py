"""真实HTTP→业务服务→MySQL→模型记忆与压缩验收；输入明确为脚本模拟学生。"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

import httpx
from request_protocol import request_body as native_request_body

_request_bindings = {}

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from memory_context_audit import read_audit, source_digest, usage_totals, verify_stress_trace

PREFERENCE = "以后先给提示，再给例子，最后完整解答。"
PROBLEM = "求解一元一次方程4(x-2)=12，并用代入原式的方法检查自己的计算。"


def stress_thought(index: int) -> str:
    """只从公开方程编排思考，不查询内部参考解；用于输入增长验收。"""
    topics = [
        "我在检查括号前面的4究竟作用在哪些项上，还没有提交最终解。",
        "我在区分等号两边同时进行同一种运算，与只改动左边的一项。",
        "我在检查常数项的符号，避免把括号中的减法看成无关的装饰。",
        "我在比较先去括号和先把两边同时除以4，两条路径为什么等价。",
        "我在考虑每写一行等式后怎样解释这一行与上一行的关系。",
        "我在安排最后的代入检查，但现在仍先检查当前变形的理由。",
    ]
    rows = [f"【工程压力验收的脚本模拟思考，第{index + 1}轮；不代表真实学生学习数据】", topics[index % len(topics)]]
    for part in range(16):
        rows.append(
            f"检查记录{index + 1}-{part + 1}：题目始终是4(x-2)=12。我先在草稿中分别辨认未知数、常数、括号以及等号，"
            "再检查自己有没有把乘法遗漏在括号中的某一项。看到同一个式子时，我会重新说明操作发生在哪一边，"
            "以及相同操作为什么需要同时作用在等式两边。我还没有交出完整解，也没有把读过提示当作自己独立完成。"
        )
    rows.append("请继续围绕这道原题，只给一个需要我检查的小问题，暂时不要换题。")
    text = "\n".join(rows)
    assert len(text) < 5000
    return text


def render_dialogue(report):
    lines = ["记忆与上下文压缩真实后端验收对话", "", "学生输入均为脚本编排的模拟作答与压力文本；教学、记忆提取、摘要由实际配置模型执行。",
             "本次检查工程不变量，不构成真实学生实验或性能基准。", ""]
    for turn in report.get("turns", []):
        lines.extend([f"{turn['index']}. {turn['label']}（会话 {turn['session_id']}）", "模拟学生：", turn["student_text"],
                      "", "真实后端公开回复：", turn.get("teacher_text", "[未发布或失败]"), ""])
    lines.extend(["完成状态：" + str(report.get("complete", False)), "失败：" + json.dumps(report.get("failure"), ensure_ascii=False), ""])
    return "\n".join(lines)


async def main(args):
    # 审计进程只读同一个隔离验收数据库，不创建或填充任何历史事实。
    os.environ["TUTOR_DATABASE"] = args.database
    student = "evaluation_" + uuid4().hex[:12]
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(), "student_id": student,
        "base_url": args.base_url, "database": args.database,
        "backend_source_sha256": source_digest(),
        "script_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "audit_helper_sha256": sha256(Path(__file__).with_name("memory_context_audit.py").read_bytes()).hexdigest(),
        "data_origin": "scripted_synthetic_student_responses_and_disclosed_context_stress_text",
        "execution": {"concurrency": 1, "http_retry_policy": "none", "http_timeout_seconds": 240,
                      "concurrency_scope": "sequential_HTTP_driver; background_memory_worker_may_overlap",
                      "database_writes_from_script": 0, "model_mocking": False},
        "not_tested": ["真实学生学习增益", "长期保持", "压缩事实召回的人工标注评测", "容量或费用基准", "多进程后台worker吞吐"],
        "sessions": {}, "checks": [], "requests": [], "turns": [], "complete": False,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    dialogue_path = args.report.with_name(args.report.stem + "_dialogue.txt")

    def save():
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        dialogue_path.write_text(render_dialogue(report), encoding="utf-8")

    async def audit():
        return await asyncio.to_thread(read_audit, student, list(report["sessions"].values()))

    async with httpx.AsyncClient(base_url=args.base_url.rstrip("/") + "/api", timeout=240, trust_env=False) as client:
        async def request(method, path, body=None):
            body = await native_request_body(client, method, path, body, _request_bindings)
            started = time.perf_counter()
            item = {"method": method, "path": path, "request_id": (body or {}).get("request_id"), "attempt": 1}
            try:
                response = await client.request(method, path, json=body)
                item["status"] = response.status_code
                item["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
                report["requests"].append(item)
                if response.status_code != 200:
                    raise RuntimeError(f"HTTP {response.status_code}: {response.text[:1000]}")
                return response.json()
            except Exception as exc:
                item.setdefault("duration_ms", round((time.perf_counter() - started) * 1000, 2))
                item["error"] = type(exc).__name__
                if item not in report["requests"]:
                    report["requests"].append(item)
                save()
                raise

        async def check(label, operation):
            started = time.perf_counter()
            try:
                detail = await operation()
                report["checks"].append({"label": label, "status": "passed", "details": detail,
                                         "duration_ms": round((time.perf_counter() - started) * 1000, 2)})
                print("PASS " + label, flush=True)
            except Exception as exc:
                report["checks"].append({"label": label, "status": "failed", "error": str(exc)[:1500]})
                raise
            finally:
                save()

        async def create(name, goal):
            data = await request("POST", "/sessions", {"student_id": student, "course_id": "junior_algebra", "goal": goal, "concept_ids": []})
            report["sessions"][name] = data["id"]
            return data

        async def tutoring(sid, action, text="", question_id=None):
            snapshot = await request("GET", f"/sessions/{sid}")
            body = {"action": action, "text": text, "request_id": str(uuid4()), "expected_version": snapshot["version"]}
            if question_id:
                body["question_id"] = question_id
            turn = {"index": len(report["turns"]) + 1, "label": "tutoring/" + action, "session_id": sid, "student_text": text or action}
            report["turns"].append(turn)
            data = await request("POST", f"/sessions/{sid}/tutoring?stream=false", body)
            turn["teacher_text"] = data.get("message", {}).get("content", "")
            turn["policy"] = data.get("message", {}).get("metadata", {}).get("policy")
            return data, body

        async def completed_memory(sid, path, body=None):
            result = await request("POST", f"/sessions/{sid}/memory/{path}?wait=true", body)
            job_id = result.get("job_id")
            # wait=true可能遇到已被后台领取的任务；只轮询现有任务，绝不重复触发提取。
            deadline = time.monotonic() + 240
            while True:
                data = await request("GET", f"/sessions/{sid}/memory")
                job = next((job for job in data["jobs"] if job["id"] == job_id), None)
                if job and job["status"] == "completed":
                    return data, job
                if job and job["status"] in {"failed", "superseded"}:
                    raise AssertionError("目标记忆任务未成功：" + json.dumps(job, ensure_ascii=False))
                if time.monotonic() >= deadline:
                    raise TimeoutError("记忆任务未在观察窗口完成")
                await asyncio.sleep(2)

        try:
            first = await create("learning", "模拟学生学习含括号的一元一次方程，并保留本会话的补救返回点")
            sid = first["id"]
            root_id = first["session_goals"]["root_goal_id"]

            async def original_and_return():
                start, _ = await tutoring(sid, "start", "开始这道题。" + PREFERENCE, "junior_algebra:negative_brackets")
                assert start["tutoring"]["equation"] == "-2(x-3)=8"
                before = await request("GET", f"/sessions/{sid}")
                active = before["session_goals"]["active_goal_id"]
                wrong, _ = await tutoring(sid, "step", "-2x-6=8")
                assert wrong["verdict"]["result"] == "incorrect" and wrong["tutoring"]["probe_prompt"]
                supplement = await request("GET", f"/sessions/{sid}")
                assert len(supplement["session_goals"]["return_stack"]) == 1
                point = supplement["session_goals"]["return_stack"][0]
                assert point["goal_id"] == active and point["current_step"] == "-2(x-3)=8"
                correct, _ = await tutoring(sid, "step", "6")
                restored = await request("GET", f"/sessions/{sid}")
                assert correct["tutoring"]["equation"] == "-2(x-3)=8" and correct["tutoring"]["current_equation"] == "-2(x-3)=8"
                assert restored["session_goals"]["active_goal_id"] == active and restored["session_goals"]["return_stack"] == []
                hint, _ = await tutoring(sid, "hint")
                policy = hint["message"]["metadata"]["policy"]
                assert policy["source"] == "agent", policy
                for line in ("-2x+6=8", "-2x=2", "x=-1"):
                    result, _ = await tutoring(sid, "step", line)
                    assert result["verdict"]["result"] == "correct"
                assert result["tutoring"]["status"] == "ready_for_transfer"
                return {"root_goal_id": root_id, "restored_goal_id": active, "same_original_equation": True,
                        "hint_run_id": policy["run_id"], "no_independent_mastery_claim": True}

            await check("M01 目标结构持久化、补救返回同题同一步且实际模型提示", original_and_return)

            async def extract_and_close():
                baseline = await audit()
                data, job = await completed_memory(sid, "refresh", {"close": False})
                assert data["student_memory"] and data["student_memory"]["content"]
                assert "提示" in data["student_memory"]["content"] and "完整" in data["student_memory"]["content"]
                assert data["session_experience"] and data["session_experience"]["status"] == "provisional"
                report["memory_before_close"] = data
                data, final_job = await completed_memory(sid, "close")
                assert data["session_experience"]["status"] == "final"
                assert data["session_experience"]["covered_seq"] == data["fact_seq"]
                after = await audit()
                assert after["student_state_sha256"] == baseline["student_state_sha256"]
                assert after["experience_counts"][sid] == 1 and after["fact_source_unique"]
                report["memory_after_close"] = data
                return {"refresh_job": job["id"], "close_job": final_job["id"], "experience_count": 1,
                        "covered_seq": data["fact_seq"], "student_memory_version": data["student_memory"]["version"],
                        "student_state_unchanged_by_memory": True}

            await check("M02 实际模型提取两类记忆、会话唯一经历关闭为final且不改掌握度", extract_and_close)

            async def cross_session_and_replay():
                fresh = await create("fresh", "新会话只查询历史经历，并进行一次提示交互")
                fresh_sid = fresh["id"]
                assert fresh["session_goals"]["root_goal_id"] != root_id
                assert len(fresh["session_goals"]["nodes"]) == 1 and fresh["session_goals"]["return_stack"] == []
                assert fresh["session_goals"]["active_goal_id"] == fresh["session_goals"]["root_goal_id"]
                await tutoring(fresh_sid, "start", "开始新会话，只在此会话建立目标。", "junior_algebra:negative_brackets")
                hint, body = await tutoring(fresh_sid, "hint")
                policy = hint["message"]["metadata"]["policy"]
                assert policy["source"] == "agent", policy
                before = await audit()
                loaded = [event["data"] for event in before["events"] if event["event"] == "memory_context_loaded" and event["data"].get("run_id") == policy["run_id"]]
                assert loaded, "没有可证明实际输入加载记忆的审计回执"
                receipt = loaded[-1]
                assert receipt.get("student_memory_present") and receipt.get("student_memory_version", 0) > 0
                experience_ids = receipt.get("session_experience_ids", [])
                assert sid in experience_ids, receipt
                replay = await request("POST", f"/sessions/{fresh_sid}/tutoring?stream=false", body)
                assert replay.get("replayed")
                after = await audit()
                assert after["fact_counts"] == before["fact_counts"] and after["message_counts"] == before["message_counts"]
                assert len(after["runs"]) == len(before["runs"])
                return {"new_root_goal_id": fresh["session_goals"]["root_goal_id"], "memory_input_receipt": receipt,
                        "same_request_replay_new_facts": 0, "same_request_replay_new_messages": 0, "same_request_replay_new_model_runs": 0}

            await check("M03 新会话不继承目标、真实提示加载历史记忆、重试不新增事实", cross_session_and_replay)

            async def guidance_compression():
                stress = await create("stress", "工程验收模拟：在同一会话长对话中保留方程主线与待答状态")
                stress_sid = stress["id"]
                initial_problem_id = None
                assisted_before = False
                for index in range(args.guidance_turns + 1):
                    state = await request("GET", f"/sessions/{stress_sid}")
                    text = ("这是工程验收的模拟学习请求。请引导我检查这道方程，先提出一个小问题，不直接给完整解。" if index == 0 else stress_thought(index - 1))
                    body = {"action": "start" if index == 0 else "reply", "text": text,
                            "request_id": str(uuid4()), "expected_version": state["version"]}
                    if index == 0:
                        body["problem"] = {"stem": PROBLEM, "source_kind": "pasted"}
                    turn = {"index": len(report["turns"]) + 1, "label": "guidance/stress-" + str(index),
                            "session_id": stress_sid, "student_text": text}
                    report["turns"].append(turn)
                    data = await request("POST", f"/sessions/{stress_sid}/guidance?stream=false", body)
                    turn["teacher_text"] = data.get("message", {}).get("content", "")
                    current = await request("GET", f"/sessions/{stress_sid}")
                    guidance = current["guidance"]
                    initial_problem_id = initial_problem_id or guidance["problem"]["id"]
                    assert guidance["problem"]["id"] == initial_problem_id and guidance["problem"]["stem"] == PROBLEM
                    assert current["session_goals"]["root_goal_id"] == stress["session_goals"]["root_goal_id"]
                    assert isinstance(guidance["assisted"], bool) and isinstance(guidance["help_level"], int)
                    if assisted_before:
                        assert guidance["assisted"], "压缩后帮助状态发生回退"
                    assisted_before = guidance["assisted"]
                    assert guidance.get("pending_question_id") and guidance.get("pending_question"), "当前完整待答问题没有保留"
                    snapshot = await audit()
                    evidence = next(item for item in snapshot["guidance_integrity"] if item["session_id"] == stress_sid)
                    assert evidence["evidence_count"] == index + 1
                    save()
                snapshot = await audit()
                return verify_stress_trace(report, snapshot, PROBLEM)

            await check("C01 长会话实际模型压缩、检查点落库、原题待答帮助及原话保留", guidance_compression)

            async def background_jobs_settled():
                # HTTP完成不等于后台记忆完成；只轮询本次模拟学生的既有任务，不重复入队。
                deadline = time.perf_counter() + 120
                while True:
                    snapshot = await audit()
                    jobs = snapshot["jobs"]
                    waiting = [job for job in jobs if job["status"] in {"pending", "running", "retry"}]
                    if not waiting:
                        failed = [job for job in jobs if job["status"] == "failed"]
                        assert not failed, "本轮后台记忆有失败终态：" + json.dumps(
                            [{"id": job["id"], "error": job.get("error")} for job in failed], ensure_ascii=False)
                        return {"job_count": len(jobs), "statuses": {status: sum(
                            job["status"] == status for job in jobs) for status in sorted({job["status"] for job in jobs})},
                            "all_owned_jobs_terminal": True}
                    if time.perf_counter() >= deadline:
                        raise AssertionError("后台记忆任务120秒内未收敛，不能把HTTP结束当作完整验收")
                    await asyncio.sleep(1)

            await check("A01 本轮后台记忆任务收敛且无失败终态", background_jobs_settled)
            report["complete"] = True
        except Exception as exc:
            report["failure"] = {"type": type(exc).__name__, "message": str(exc)[:2500]}
            print("FAIL " + type(exc).__name__ + ": " + str(exc)[:1000], flush=True)
        finally:
            try:
                report["audit"] = await audit()
                report["model_usage"] = usage_totals(report["audit"])
                report["backend_source_sha256_after"] = source_digest()
                report["source_unchanged_during_acceptance"] = report["backend_source_sha256_after"] == report["backend_source_sha256"]
                if not report["source_unchanged_during_acceptance"]:
                    report["complete"] = False
                    report["source_change_failure"] = "验收期间后端源码发生变化，需要基于冻结版本重跑。"
            except Exception as exc:
                report["audit_failure"] = {"type": type(exc).__name__, "message": str(exc)[:800]}
                report["complete"] = False
            report["ended_at"] = datetime.now(timezone.utc).isoformat()
            save()
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8011")
    parser.add_argument("--database", default="stem_tutor_memory_acceptance")
    parser.add_argument("--guidance-turns", type=int, default=6)
    parser.add_argument("--report", type=Path, default=ROOT / "data/reports/memory_context_acceptance.json")
    arguments = parser.parse_args()
    if not 4 <= arguments.guidance_turns <= 10:
        parser.error("--guidance-turns必须为4至10，用于真实请求数量有界的验收")
    raise SystemExit(asyncio.run(main(arguments)))
