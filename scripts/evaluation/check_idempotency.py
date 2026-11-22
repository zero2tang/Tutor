"""用独立验收学生验证 v2 请求回放，不从历史 Attempt 猜测停点。"""

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from uuid import uuid4

import httpx
from request_protocol import request_body

ROOT = Path(__file__).resolve().parents[2]


async def main() -> None:
    student = "evaluation_" + uuid4().hex[:12]
    bindings = {}
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8008/api", timeout=660, trust_env=False) as client:
        async def request(method, path, body=None):
            body = await request_body(client, method, path, body, bindings)
            response = await client.request(method, path, json=body)
            response.raise_for_status()
            return response.json()

        session = await request("POST", "/sessions", {"course_id": "math", "student_id": student,
            "goal": "工程验收模拟学生：完成指定乘法原理题后等待，不自动出题。", "concept_ids": ["math:c0"]})
        sid = session["id"]
        await request("POST", f"/sessions/{sid}/practice?stream=false", {"question_id": "math:0",
            "cognitive_type": "understanding", "request_id": str(uuid4())})
        # 停点只绑定一次。完成后的回放继续使用原 wait_id，不能改绑后续题。
        body = await request_body(client, "POST", f"/sessions/{sid}/answers?stream=false",
            {"answer": "24", "request_id": str(uuid4())}, bindings)
        first = await request("POST", f"/sessions/{sid}/answers?stream=false", body)
        before = await request("GET", "/student?course_id=math&student_id=" + student)
        repeated = await request("POST", f"/sessions/{sid}/answers?stream=false", body)
        after = await request("GET", "/student?course_id=math&student_id=" + student)
        assert repeated["replayed"] and repeated["attempt_id"] == first["attempt_id"]
        assert before == after
        mismatch = await client.post(f"/sessions/{sid}/answers?stream=false", json={**body, "answer": "另一份答案"})
        assert mismatch.status_code in {400, 409}, mismatch.text
    result = {"generated_at": datetime.now(timezone.utc).isoformat(), "status": "passed",
        "student_id": student, "session_id": sid, "attempt_id": first["attempt_id"],
        "same_attempt_replayed": True, "states_unchanged": True, "changed_answer_rejected": True,
        "original_wait_id": body["wait_id"], "data_origin": "disclosed_scripted_student_answer"}
    target = ROOT / "data/reports/supplemental_idempotency.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
