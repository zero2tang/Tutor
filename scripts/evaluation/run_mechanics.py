"""通过真实 API 执行固定 20 任务，保留失败项与逐任务耗时。"""

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


def source_fingerprint() -> str:
    """固定输入文件与实现版本，便于把结果追溯到本次代码。"""
    files = sorted((ROOT / "backend/app").rglob("*.py"))
    files += sorted((ROOT / "backend/app/agent/skills").rglob("*.md"))
    files += [ROOT / "data/evaluation/mechanics_20_tasks.json", Path(__file__)]
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


class Acceptance:
    def __init__(self, base: str):
        self.base = base
        self.client = httpx.AsyncClient(base_url=base, timeout=240, trust_env=False)
        self.sessions: dict[str, str] = {}
        self.artifacts = {}
        self.student_id = "evaluation_" + uuid4().hex[:12]
        self.artifacts["student_id"] = self.student_id

    async def request(self, method: str, path: str, payload=None):
        payload = await native_request_body(self.client, method, path, payload, _request_bindings)
        if path == "/student":
            path += "?student_id=" + self.student_id
        if method == "POST" and path == "/sessions":
            payload = {**payload, "student_id": self.student_id}
        response = await self.client.request(method, path, json=payload)
        if response.is_error:
            raise AssertionError(f"HTTP {response.status_code}: {response.text[:350]}")
        return response.json()

    async def session(self, name: str, concepts=None, goal=None) -> str:
        if name not in self.sessions:
            data = await self.request(
                "POST",
                "/sessions",
                {
                    "goal": goal or "验收学习：单向应力与应变",
                    "concept_ids": concepts or ["mechanics:c187"],
                },
            )
            self.sessions[name] = data["id"]
        return self.sessions[name]

    async def message(self, sid: str, content: str, **options):
        return await self.request(
            "POST",
            f"/sessions/{sid}/messages?stream=false",
            {"content": content, "request_id": str(uuid4()), **options},
        )

    async def pose(self, name: str, qid: str, *, cognitive="recall") -> str:
        sid = await self.session(name)
        await self.request(
            "POST",
            f"/sessions/{sid}/practice?stream=false",
            {"question_id": qid, "cognitive_type": cognitive},
        )
        return sid

    async def run(self, task: dict) -> dict:
        tid = task["id"]
        if tid == "T01":
            health = await self.request("GET", "/health")
            course = next(
                item
                for item in (await self.request("GET", "/courses"))["items"]
                if item["id"] == "mechanics"
            )
            assert health["status"] == "ready" and all(
                v["status"] == "ready" for v in health["indices"].values()
            )
            assert course["question_count"] == 177 and course["concept_count"] == 198
            return {"course": course, "health": health["status"]}
        if tid in {"T02", "T03", "T04"}:
            mode = "hybrid" if tid == "T04" else "keyword"
            params = httpx.QueryParams(q=task["query"], mode=mode, limit=10)
            data = await self.request("GET", f"/search/questions?{params}")
            ids = [v["id"] for v in data["items"]]
            assert task["expected_id"] in ids, f"未命中目标题，实际结果：{ids}"
            assert not data["degraded"]
            if tid == "T04":
                assert set(data["channels_used"]) == {"keyword", "vector"}
            return {
                "rank": ids.index(task["expected_id"]) + 1,
                "timings": data["timings"],
            }
        if tid == "T05":
            data = await self.request(
                "GET",
                "/search/questions?coverage=all&concept_ids="
                + ",".join(task["concept_ids"]),
            )
            assert data["items"] and all(
                set(task["concept_ids"]).issubset(q["question"]["concept_ids"])
                for q in data["items"]
            )
            return {"matched": len(data["items"])}
        if tid == "T06":
            params = httpx.QueryParams(q=task["query"], limit=6)
            data = await self.request("GET", f"/retrieval/rag?{params}")
            assert (
                data["items"]
                and data["citations"]
                and "vector" in data["channels_used"]
            )
            assert any(
                "F/A" in v["text"] or "\\sigma" in v["text"] for v in data["items"]
            )
            return {"sources": data["citations"], "timings": data["timings"]}
        if tid == "T07":
            params = httpx.QueryParams(
                q=task["query"],
                seed_concept_ids=",".join(task["seed_concept_ids"]),
                max_hops=1,
            )
            data = await self.request("GET", f"/retrieval/graph?{params}")
            assert not data["degraded"] and "neo4j" in data["channels_used"]
            assert data["graph"]["edges"] and data["items"] and data["citations"]
            assert any(
                {edge["source"], edge["target"]} == set(task["seed_concept_ids"])
                for edge in data["graph"]["edges"]
            )
            assert all(
                edge["relation_type"] == "RELATED_TO" for edge in data["graph"]["edges"]
            )
            return {
                "nodes": len(data["graph"]["nodes"]),
                "edges": len(data["graph"]["edges"]),
                "timings": data["timings"],
            }
        if tid == "T08":
            response = await self.client.get("/search/questions?course_id=PHP&q=test")
            assert response.status_code == 400
            response = await self.client.get(
                "/retrieval/graph?q=test&seed_concept_ids=PHP:c1"
            )
            assert response.status_code == 400
            return {"cross_course_requests_rejected": 2}
        if tid == "T09":
            profile = await self.request("GET", "/student")
            unknown = [s for s in profile["states"] if s["status"] == "unassessed"]
            assert unknown and all(
                all(v is None for v in s["mastery"].values()) for s in unknown
            )
            return {"unassessed_concepts": len(unknown)}
        if tid == "T10":
            sid = await self.session("lesson")
            data = await self.message(
                sid, "请解释单向应力的定义与单位，并给出一个数值例子", intent="teach"
            )
            assert len(data["lesson"]["content"]) > 150 and data["message"]["citations"]
            assert "应力" in data["lesson"]["content"] and (
                "Pa" in data["lesson"]["content"] or "帕" in data["lesson"]["content"]
            )
            runs = (await self.request("GET", f"/sessions/{sid}/runs"))["items"]
            assert {"supervisor", "knowledge"}.issubset(
                {r["agent"] for r in runs if r["status"] == "completed"}
            )
            assert all(
                any(t["status"] == "completed" for t in r["tool_calls"])
                for r in runs
                if r["status"] == "completed"
            )
            self.artifacts["lesson_session"] = sid
            return {
                "session_id": sid,
                "characters": len(data["lesson"]["content"]),
                "run_ids": [r["id"] for r in runs],
            }
        if tid == "T11":
            sid = await self.session(
                "multi",
                ["mechanics:c19", "mechanics:c160"],
                "比较应力与应变，理解它们的区别与联系",
            )
            data = await self.message(
                sid,
                "请结合知识图谱比较应力与应变，并说明联系",
                intent="teach",
                concept_ids=["mechanics:c19", "mechanics:c160"],
            )
            assert (
                len(data["plan"]["concept_ids"]) >= 2
                and len(data["plan"]["steps"]) >= 2
            )
            assert all(term in data["lesson"]["content"] for term in ["应力", "应变"])
            runs = (await self.request("GET", f"/sessions/{sid}/runs"))["items"]
            assert any(
                t["name"] == "graph_rag_retrieve" and t["status"] == "completed"
                for r in runs
                for t in r["tool_calls"]
            )
            return {"session_id": sid, "agenda": data["plan"]["steps"]}
        if tid == "T12":
            sid = await self.session("lesson")
            before = await self.request("GET", f"/sessions/{sid}")
            data = await self.message(
                sid,
                "如果截面积不变而作用力变成原来的两倍，应力会怎样？为什么？",
                intent="teach",
            )
            after = await self.request("GET", f"/sessions/{sid}")
            assert (
                after["goal"] == before["goal"]
                and len(after["messages"]) >= len(before["messages"]) + 2
            )
            assert "应力" in data["lesson"]["content"] and (
                "两倍" in data["lesson"]["content"] or "2" in data["lesson"]["content"]
            )
            return {"session_id": sid, "message_count": len(after["messages"])}
        if tid == "T13":
            sid = await self.pose("correct", "mechanics:Pm_668268")
            data = await self.request("GET", f"/sessions/{sid}")
            assert (
                data["phase"] == "awaiting_answer"
                and data["pending_question"]["id"] == "mechanics:Pm_668268"
            )
            assert (
                "answer" not in data["pending_question"]
                and "contract" not in data
                and "prediction" not in data
            )
            runs = (await self.request("GET", f"/sessions/{sid}/runs"))["items"]
            assert {"exercise", "assessment"}.issubset(
                {r["agent"] for r in runs if r["status"] == "completed"}
            )
            return {"session_id": sid, "question_id": data["pending_question"]["id"]}
        if tid == "T14":
            sid = await self.session("correct")
            if not (await self.request("GET", f"/sessions/{sid}"))["pending_question"]:
                await self.pose("correct", "mechanics:Pm_668268")
            key = str(uuid4())
            result = await self.request(
                "POST",
                f"/sessions/{sid}/answers?stream=false",
                {"answer": task["answer"], "request_id": key},
            )
            # 先保存已经发生的作答，让幂等检查不依赖掌握度更新是否达标。
            self.artifacts.update(
                correct_session=sid,
                correct_key=key,
                correct_attempt=result["attempt_id"],
            )
            assert (
                result["result"] == "correct" and result["state_updates"]
            ), f"判分={result['result']}，诊断置信度={result['diagnosis']['confidence']}，状态更新数={len(result['state_updates'])}"
            assert all(
                value is None or value <= 1
                for change in result["state_updates"]
                for value in change["after"].values()
            )
            assert result["next_plan"]["message"] and result["diagnosis"]["reflection"]
            return {
                "attempt_id": result["attempt_id"],
                "result": result["result"],
                "state_updates": result["state_updates"],
            }
        if tid == "T15":
            sid = await self.pose("wrong", "mechanics:Pm_668269")
            result = await self.request(
                "POST",
                f"/sessions/{sid}/answers?stream=false",
                {"answer": task["answer"], "request_id": str(uuid4())},
            )
            assert (
                result["result"] == "incorrect"
                and result["diagnosis"]["feedback"]
                and result["next_plan"]["message"]
            )
            return {
                "session_id": sid,
                "attempt_id": result["attempt_id"],
                "misconception": result["diagnosis"]["misconception"],
            }
        if tid == "T16":
            assert self.artifacts.get("correct_key"), "需要 T14 先完成"
            sid = self.artifacts["correct_session"]
            before = await self.request("GET", "/student")
            result = await self.request(
                "POST",
                f"/sessions/{sid}/answers?stream=false",
                {"answer": "C", "request_id": self.artifacts["correct_key"]},
            )
            after = await self.request("GET", "/student")
            assert (
                result["replayed"]
                and result["attempt_id"] == self.artifacts["correct_attempt"]
            )
            assert (
                before["attempt_count"] == after["attempt_count"]
                and before["states"] == after["states"]
            )
            return {
                "same_attempt_id": result["attempt_id"],
                "attempt_count_unchanged": True,
            }
        if tid == "T17":
            sid = await self.pose("fill", "mechanics:Pm_1256155")
            result = await self.request(
                "POST",
                f"/sessions/{sid}/answers?stream=false",
                {"answer": task["answer"], "request_id": str(uuid4())},
            )
            assert result["result"] == "correct" and result["score"] == 1
            return {"attempt_id": result["attempt_id"], "result": result["result"]}
        if tid == "T18":
            sid = await self.session(
                "generated", ["mechanics:c187"], "应用单向应力公式计算"
            )
            result = await self.message(
                sid,
                "生成一道使用应力公式计算的四选一题，参数和单位要完整",
                intent="practice",
                generate=True,
                cognitive_type="application",
            )
            question = result["question"]
            assert (
                question["generated"]
                and set(question["options"]) == {"A", "B", "C", "D"}
                and "answer" not in question
            )
            runs = (await self.request("GET", f"/sessions/{sid}/runs"))["items"]
            exercise = [
                r
                for r in runs
                if r["agent"] == "exercise" and r["status"] == "completed"
            ]
            assert len(exercise) >= 2 and any(not r["tool_calls"] for r in exercise)
            self.artifacts["generated_question"] = question
            return {
                "session_id": sid,
                "question": question,
                "independent_solver_runs": len(exercise),
            }
        if tid == "T19":
            scenarios = []
            for intent, qid, content, answer in [
                (
                    "explain",
                    "mechanics:Pm_668267",
                    "请讲解当前题目的答案和建模理由",
                    "A",
                ),
                (
                    "teach",
                    "mechanics:Pm_668268",
                    "请讲解单向应力的定义、单位和计算例子",
                    "C",
                ),
            ]:
                sid = await self.pose("assisted_" + intent, qid)
                await self.message(sid, content, intent=intent)
                data = await self.request("GET", f"/sessions/{sid}")
                assert data["pending_assisted"], f"{intent} 后未标记为辅助练习"
                result = await self.request(
                    "POST",
                    f"/sessions/{sid}/answers?stream=false",
                    {"answer": answer, "request_id": str(uuid4())},
                )
                assert result["assisted"] and result["state_updates"] == []
                scenarios.append(
                    {
                        "intent": intent,
                        "attempt_id": result["attempt_id"],
                        "assisted": True,
                        "independent_state_updates": 0,
                    }
                )
            return {"scenarios": scenarios}
        if tid == "T20":
            sid = await self.pose("recovery", "mechanics:Pm_668265")
            old = await self.request("GET", f"/sessions/{sid}")
            async with httpx.AsyncClient(
                base_url=self.base, timeout=30, trust_env=False
            ) as fresh:
                response = await fresh.get(f"/sessions/{sid}")
                response.raise_for_status()
                recovered = response.json()
            assert (
                old["pending_question"] == recovered["pending_question"]
                and recovered["phase"] == "awaiting_answer"
            )
            events = (await self.request("GET", f"/sessions/{sid}/events"))["items"]
            assert any(e["event"] == "question" for e in events) and any(
                e["event"] == "done" for e in events
            )
            self.artifacts["recovery_session"] = sid
            return {
                "session_id": sid,
                "question_id": recovered["pending_question"]["id"],
                "persisted_events": len(events),
            }
        raise ValueError("未实现任务：" + tid)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8008/api")
    parser.add_argument("--only", default="")
    parser.add_argument(
        "--output", type=Path, help="为本次回归另存结果，避免覆盖历史验收"
    )
    args = parser.parse_args()
    suite = json.loads(
        (ROOT / "data/evaluation/mechanics_20_tasks.json").read_text(encoding="utf-8")
    )
    selected = set(args.only.split(",")) if args.only else None
    output = args.output or (
        ROOT
        / "data/reports"
        / (
            "mechanics_acceptance_debug.json"
            if selected
            else "mechanics_acceptance.json"
        )
    )
    acceptance = Acceptance(args.base_url)
    fingerprint = source_fingerprint()
    results = []
    started = time.perf_counter()
    try:
        for task in suite["tasks"]:
            if selected and task["id"] not in selected:
                continue
            tick = time.perf_counter()
            record = {
                "id": task["id"],
                "name": task["name"],
                "category": task["category"],
            }
            try:
                record.update(status="passed", evidence=await acceptance.run(task))
            except Exception as exc:
                record.update(
                    status="failed", error=f"{type(exc).__name__}: {str(exc)[:900]}"
                )
            record["duration_seconds"] = round(time.perf_counter() - tick, 2)
            results.append(record)
            passed = sum(row["status"] == "passed" for row in results)
            report = {
                "suite_version": suite["version"],
                "source_sha256": fingerprint,
                "complete_suite_run": not selected
                and len(results) == len(suite["tasks"]),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "passed": passed,
                "total": len(results),
                "success_rate": passed / len(results),
                "target_success_rate": suite["target_success_rate"],
                "duration_seconds": round(time.perf_counter() - started, 2),
                "tasks": results,
                "artifacts": acceptance.artifacts,
            }
            output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(
                f"{task['id']} {record['status']} {task['name']} ({record['duration_seconds']}s)",
                flush=True,
            )
            if record["status"] == "failed":
                print(record["error"], flush=True)
        print(
            f"RESULT {passed}/{len(results)} = {passed / max(1, len(results)):.0%}",
            flush=True,
        )
        if (
            not selected
            and passed / max(1, len(results)) < suite["target_success_rate"]
        ):
            raise SystemExit(1)
    finally:
        await acceptance.client.aclose()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
