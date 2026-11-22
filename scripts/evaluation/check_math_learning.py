"""使用真实数学题验证四角色链路，所有写入属于独立验收学生。"""

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

import httpx
from request_protocol import request_body as native_request_body

_request_bindings = {}

ROOT = Path(__file__).resolve().parents[2]


async def main():
    student_id = "evaluation_" + uuid4().hex[:12]
    report = {"student_id": student_id, "course_id": "math", "started_at": datetime.now(timezone.utc).isoformat(), "checks": []}
    output = ROOT / "data/reports/math_learning_smoke.json"
    started = time.perf_counter()

    def save():
        report["duration_seconds"] = round(time.perf_counter() - started, 2)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    async with httpx.AsyncClient(base_url="http://127.0.0.1:8008/api", timeout=240, trust_env=False) as client:
        async def request(method, path, body=None):
            body = await native_request_body(client, method, path, body, _request_bindings)
            response = await client.request(method, path, json=body)
            response.raise_for_status()
            return response.json()

        async def new_session(qid, goal):
            found = await request("GET", "/search/questions?course_id=math&q=" + qid)
            question = found["items"][0]["question"]
            assert question["id"] == qid and "answer" not in question and "explanation" not in question
            created = await request("POST", "/sessions", {"course_id": "math", "student_id": student_id, "goal": goal, "concept_ids": question["concept_ids"]})
            return created["id"]

        try:
            sid = await new_session("math:0", "理解乘法原理，分清步骤之间是否相互独立")
            lesson = await request("POST", f"/sessions/{sid}/messages?stream=false", {"content": "请以分步骤选择为例解释乘法原理，说明什么时候不能直接相乘。", "intent": "teach", "request_id": str(uuid4())})
            assert len(lesson["lesson"]["content"]) > 150 and lesson["message"]["citations"]
            report["checks"].append({"id": "M01", "status": "passed", "check": "数学主Agent与知识点Agent的真实教学", "session_id": sid, "characters": len(lesson["lesson"]["content"])})
            save()
            posed = await request("POST", f"/sessions/{sid}/practice?stream=false", {"question_id": "math:0", "cognitive_type": "recall"})
            assert posed["question"]["id"] == "math:0" and "answer" not in posed["question"]
            result = await request("POST", f"/sessions/{sid}/answers?stream=false", {"answer": "24", "request_id": "math-single-" + uuid4().hex})
            assert result["result"] == "correct" and result["score"] == 1 and result["diagnosis"]["reflection"]
            report["checks"].append({"id": "M02", "status": "passed", "check": "原始单空题经过预测、判分、反思和下一步决策", "attempt_id": result["attempt_id"], "state_update_count": len(result["state_updates"])})
            save()
            multi = await new_session("math:7644", "按空位顺序完成两空数学题")
            await request("POST", f"/sessions/{multi}/practice?stream=false", {"question_id": "math:7644", "cognitive_type": "recall"})
            key = "math-multiple-" + uuid4().hex
            answer = await request("POST", f"/sessions/{multi}/answers?stream=false", {"answer": "60；20", "request_id": key})
            assert answer["result"] == "correct" and answer["score"] == 1
            report["checks"].append({"id": "M03", "status": "passed", "check": "真实数学多空按顺序判分", "question_id": "math:7644", "attempt_id": answer["attempt_id"]})
            before = await request("GET", f"/student?course_id=math&student_id={student_id}")
            replay = await request("POST", f"/sessions/{multi}/answers?stream=false", {"answer": "60；20", "request_id": key})
            after = await request("GET", f"/student?course_id=math&student_id={student_id}")
            assert replay["replayed"] and replay["attempt_id"] == answer["attempt_id"] and before == after
            runs = [run for session in [sid, multi] for run in (await request("GET", f"/sessions/{session}/runs"))["items"]]
            assert {"supervisor", "knowledge", "exercise", "assessment"}.issubset({run["agent"] for run in runs if run["status"] == "completed"})
            assert all(state["concept_id"].startswith("math:") for state in after["states"])
            report["checks"].append({"id": "M04", "status": "passed", "check": "数学幂等与四角色/档案课程范围", "agent_runs": len(runs), "attempt_count": after["attempt_count"]})
            report["complete"] = True
            report["status"] = "passed"
        except Exception as exc:
            report.update(status="failed", complete=False, error=type(exc).__name__, message=str(exc)[:600])
            save()
            raise
        finally:
            save()
    print(json.dumps({"status": report["status"], "passed": len(report["checks"]), "report": str(output)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
