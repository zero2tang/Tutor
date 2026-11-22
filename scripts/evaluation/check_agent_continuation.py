"""用独立验收档案验证真实模型的子 Agent 返回与长期任务接续。"""

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
REPORT = ROOT / "data/reports/agent_continuation_acceptance.json"


async def main():
    student = "evaluation_" + uuid4().hex[:12]
    goal = "分阶段学会乘法原理：先理解分类与分步，再分析报名例题，最后由我独立作答检验。保持这个长期学习任务。"
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "student_id": student,
              "goal": goal, "checks": [], "complete": False}
    digest = hashlib.sha256()
    for path in sorted((ROOT / "backend/app").rglob("*.py")):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    report["backend_source_sha256"] = digest.hexdigest()

    def save():
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    async with httpx.AsyncClient(base_url="http://127.0.0.1:8008/api", timeout=660, trust_env=False) as client:
        async def request(method, path, body=None):
            body = await native_request_body(client, method, path, body, _request_bindings)
            response = await client.request(method, path, json=body)
            response.raise_for_status()
            return response.json()

        async def check(identifier, label, operation):
            started = time.perf_counter()
            try:
                details = await operation()
                report["checks"].append({"id": identifier, "title": label, "status": "passed", "details": details,
                                         "duration_seconds": round(time.perf_counter() - started, 2)})
                print(identifier + " passed " + label, flush=True)
            except Exception as exc:
                report["checks"].append({"id": identifier, "title": label, "status": "failed", "error": str(exc)[:1000]})
                save()
                raise
            save()

        session = await request("POST", "/sessions", {"course_id": "math", "student_id": student,
                                                       "goal": goal, "concept_ids": ["math:c0"]})
        sid = session["id"]
        report["session_id"] = sid

        async def snapshot():
            return await request("GET", f"/sessions/{sid}")

        async def runs():
            return (await request("GET", f"/sessions/{sid}/runs"))["items"]

        async def lesson():
            result = await request("POST", f"/sessions/{sid}/messages?stream=false", {
                "content": "先完成第一阶段的知识讲解，本轮不出题。讲解后请指出学习计划做到哪里、下一步是什么，等我回复后继续。",
                "intent": "teach", "request_id": str(uuid4())})
            history = await runs()
            parent = next(r for r in history if r["agent"] == "supervisor")
            assert any(t["name"] == "call_knowledge_agent" and t["status"] == "completed" for t in parent["tool_calls"])
            state = await snapshot()
            assert result["message"]["agent"] == "supervisor"
            assert state["goal"] == goal and state["learning_progress"]["next_step"]
            assert state["active_turn_id"] is None
            return {"parent_run_id": parent["id"], "tool_calls": parent["tool_calls"], "progress": state["learning_progress"]}

        await check("A01", "知识子Agent工具返回主Agent并接续长期任务", lesson)
        await request("POST", f"/sessions/{sid}/practice?stream=false", {"question_id": "math:0", "cognitive_type": "understanding"})

        async def nested():
            previous = {r["id"] for r in await runs()}
            result = await request("POST", f"/sessions/{sid}/messages?stream=false", {
                "content": "进入长期任务第二阶段：请先由知识点老师围绕当前例题进行知识教学；教学过程中请知识点老师调用习题老师讲解当前例题的解题过程，读取题解后再总结限制条件，最后交回主老师接续原学习计划。本轮不要新出题。",
                "intent": "teach", "request_id": str(uuid4())})
            fresh = [r for r in await runs() if r["id"] not in previous]
            parent = next(r for r in fresh if r["agent"] == "supervisor")
            knowledge = next(r for r in fresh if r["agent"] == "knowledge")
            assert any(t["name"] == "call_knowledge_agent" and knowledge["id"] in t.get("child_run_ids", []) for t in parent["tool_calls"])
            nested_calls = [t for t in knowledge["tool_calls"] if t["name"] == "call_exercise_agent" and t["status"] == "completed"]
            assert nested_calls, "知识点Agent未实际委派习题Agent"
            assert any(r["agent"] == "exercise" and r["id"] in nested_calls[0]["child_run_ids"] for r in fresh)
            state = await snapshot()
            assert state["goal"] == goal and result["message"]["agent"] == "supervisor"
            assert state["pending_question"]["id"] == "math:0" and state["pending_assisted"]
            assert state["learning_progress"]["status"] != "completed"
            return {"parent_run_id": parent["id"], "knowledge_run_id": knowledge["id"], "nested_calls": nested_calls,
                    "progress": state["learning_progress"], "pending_assisted": True}

        await check("A02", "知识点→习题→知识点→主Agent的真实嵌套返回", nested)
        key = str(uuid4())
        answer_result = None

        async def answer():
            nonlocal answer_result
            answer_result = await request("POST", f"/sessions/{sid}/answers?stream=false", {"answer": "24", "request_id": key})
            assert answer_result["result"] == "correct" and answer_result["assisted"]
            assert answer_result["state_updates"] == []
            assert "next_plan" in answer_result and not answer_result.get("continuation_error")
            assert answer_result["next_plan"]["continuation"]["next_step"]
            return {"attempt_id": answer_result["attempt_id"], "assisted": True,
                    "state_update_count": 0, "next_plan": answer_result["next_plan"]}

        await check("A03", "真实评估后回主Agent，辅助回答不增加掌握证据", answer)

        async def replay():
            before = {r["id"] for r in await runs()}
            repeated = await request("POST", f"/sessions/{sid}/answers?stream=false", {"answer": "24", "request_id": key})
            assert repeated["replayed"] and repeated["attempt_id"] == answer_result["attempt_id"]
            assert repeated["next_plan"] == answer_result["next_plan"]
            assert {r["id"] for r in await runs()} == before
            return {"same_attempt": True, "same_next_plan": True, "new_agent_runs": 0}

        await check("A04", "重复提交回放评估与主Agent安排，不重复调用", replay)
        report["complete"] = True
        report["summary"] = {"passed": len(report["checks"]), "total": 4}
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        save()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
