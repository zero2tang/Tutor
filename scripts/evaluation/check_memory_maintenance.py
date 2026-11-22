"""真实 HTTP 验收记忆纠正与撤销；数据库只读审计，不注入历史或模拟模型。"""

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

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from memory_context_audit import digest_json, read_audit, source_digest, usage_totals

INITIAL_PREFERENCE = "这是脚本模拟学生的工程验收。我长期的学习偏好是先给一个简短提示，等我尝试后再给例子，最后才给完整解答。"
CORRECTION = "纠正我此前的长期学习偏好：以后我希望先看完整解答，再自己回头分析步骤；不要再把“先给提示、最后才给完整解答”作为我的现行偏好。请以这条明确要求替换旧偏好。"


def original_fingerprints(student_id: str, session_id: str) -> dict:
    """只 SELECT 既有业务行并散列，不向报告导出任何私有判分内容。"""
    from sqlalchemy import inspect, select
    from app.infrastructure.persistence.database import db_session
    from app.domains.sessions.models import LearningSession, Message
    from app.domains.students.models import StudentState

    with db_session() as db:
        session = db.get(LearningSession, session_id)
        if session is None or session.student_id != student_id:
            raise ValueError("审计会话不属于本次模拟学生")
        result = {}
        for model, scope in (
            (Message, Message.session_id == session_id),
            (StudentState, StudentState.student_id == student_id),
        ):
            rows = list(db.scalars(select(model).where(scope).order_by(model.id)))
            columns = [attribute.key for attribute in inspect(model).column_attrs]
            values = [{name: getattr(row, name) for name in columns} for row in rows]
            result[model.__tablename__] = {
                "count": len(rows), "ids": [row.id for row in rows], "sha256": digest_json(values),
            }
        return result


async def main(args):
    os.environ["TUTOR_DATABASE"] = args.database
    student_id = "evaluation_" + uuid4().hex[:12]
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url, "database": args.database, "student_id": student_id,
        "backend_source_sha256_before": source_digest(),
        "script_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "data_origin": "scripted_synthetic_student_preference_and_explicit_correction",
        "execution": {"database_writes_from_script": 0, "model_mocking": False,
                      "http_mutation_retries": 0, "http_concurrency": 1,
                      "note": "后台记忆worker可能与根验收并行；本脚本使用独立新学生，任务仅轮询不重复创建。"},
        "requests": [], "checks": [], "complete": False,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")

    async with httpx.AsyncClient(base_url=args.base_url.rstrip("/") + "/api", trust_env=False, timeout=300) as client:
        async def request(method, path, body=None):
            entry = {"method": method, "path": path, "attempt": 1, "request": body}
            started = time.perf_counter()
            try:
                response = await client.request(method, path, json=body)
                entry["http_status"] = response.status_code
                entry["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
                if response.status_code != 200:
                    entry["failure_body"] = response.text[:1500]
                    raise RuntimeError("验收 HTTP 状态异常：" + str(response.status_code))
                result = response.json()
                entry["result"] = result
                return result
            except Exception as exc:
                entry["error"] = type(exc).__name__
                raise
            finally:
                entry.setdefault("duration_ms", round((time.perf_counter() - started) * 1000, 2))
                report["requests"].append(entry)
                save()

        async def completed_memory(session_id, action, body):
            initial = await request("POST", f"/sessions/{session_id}/memory/{action}?wait=true", body)
            job_id = initial.get("job_id")
            if not job_id:
                raise AssertionError("显式记忆维护没有登记任务")
            deadline = time.monotonic() + 300
            while True:
                snapshot = await request("GET", f"/sessions/{session_id}/memory")
                job = next((item for item in snapshot["jobs"] if item["id"] == job_id), None)
                if job and job["status"] == "completed":
                    return snapshot, job
                if job and job["status"] in {"failed", "superseded"}:
                    raise AssertionError("记忆任务没有完成：" + json.dumps(job, ensure_ascii=False))
                if time.monotonic() >= deadline:
                    raise TimeoutError("记忆维护任务观察超时，未重复提交请求")
                await asyncio.sleep(2)

        async def check(label, operation):
            started = time.perf_counter()
            try:
                detail = await operation()
                report["checks"].append({"label": label, "status": "passed", "details": detail,
                                         "duration_ms": round((time.perf_counter() - started) * 1000, 2)})
                print("PASS " + label, flush=True)
            except Exception as exc:
                report["checks"].append({"label": label, "status": "failed", "error": type(exc).__name__,
                                         "detail": str(exc)[:1200]})
                raise
            finally:
                save()

        async def assert_originals_unchanged(label):
            current = await asyncio.to_thread(original_fingerprints, student_id, report["session_id"])
            report.setdefault("original_snapshots", {})[label] = current
            assert current == report["original_baseline"], "记忆维护意外改写了原始消息、辅导证据或学生掌握状态"

        try:
            session = await request("POST", "/sessions", {"student_id": student_id, "course_id": "junior_algebra",
                "goal": "学习含括号的一元一次方程；本会话用于记忆维护工程验收", "concept_ids": []})
            session_id = report["session_id"] = session["id"]
            start = await request("POST", f"/sessions/{session_id}/tutoring?stream=false", {
                "action": "start", "text": INITIAL_PREFERENCE, "request_id": str(uuid4()),
                "expected_version": session["version"], "question_id": "junior_algebra:negative_brackets"})
            assert start.get("tutoring"), "实际辅导入口未返回工作区"
            report["original_baseline"] = await asyncio.to_thread(original_fingerprints, student_id, session_id)
            assert report["original_baseline"]["messages"]["count"] >= 2
            assert report["original_baseline"]["tutoring_evidence"]["count"] >= 1
            baseline = await asyncio.to_thread(read_audit, student_id, [session_id])
            assert baseline["database"] == args.database, "只读审计连接了错误的数据库"
            report["baseline_audit"] = baseline

            async def extract_preference():
                snapshot, job = await completed_memory(session_id, "refresh", {"close": False})
                report["extracted_memory"] = snapshot
                student = snapshot["student_memory"]
                experience = snapshot["session_experience"]
                assert student and student["content"], "真实模型没有提取长期偏好"
                assert "提示" in student["content"] and "完整" in student["content"], "长期正文未保留明确偏好的主要含义"
                assert experience and experience["content"], "真实模型没有提取会话学习经历"
                assert experience["status"] == "provisional"
                assert job["metrics"].get("model_calls", 0) > 0
                await assert_originals_unchanged("after_refresh")
                return {"job_id": job["id"], "metrics": job["metrics"], "student_memory_version": student["version"],
                        "experience_version": experience["version"], "experience_session_id": experience["session_id"]}

            await check("MM01 真实模型提取明确偏好和唯一学习经历", extract_preference)

            async def correct_preference():
                before_version = report["extracted_memory"]["student_memory"]["version"]
                snapshot, job = await completed_memory(session_id, "correct", {"kind": "student", "text": CORRECTION})
                report["corrected_memory"] = snapshot
                student = snapshot["student_memory"]
                assert student and student["content"] and student["version"] > before_version
                assert "完整" in student["content"] and any(term in student["content"] for term in ("先", "优先", "首先"))
                assert any(source.startswith("correction:") for source in student["source_refs"]), "纠正后的正文没有引用明确纠正来源"
                assert snapshot["invalidation_version"] > report["extracted_memory"]["invalidation_version"]
                assert job["metrics"].get("model_calls", 0) > 0
                await assert_originals_unchanged("after_correction")
                return {"job_id": job["id"], "metrics": job["metrics"], "student_memory_version": student["version"],
                        "current_preference_text": student["content"], "semantic_review": "报告保留真实正文，关键词检查不能代替语义评审。"}

            await check("MM02 真实模型按明确纠正重建长期偏好并更新来源", correct_preference)

            async def forget_student():
                deletion = await request("DELETE", f"/sessions/{session_id}/memory/student")
                assert deletion["student_memory"]["content"] == "" and deletion["student_memory"]["source_refs"] == []
                snapshot = await request("GET", f"/sessions/{session_id}/memory")
                assert snapshot["student_memory"]["content"] == "" and snapshot["student_memory"]["source_refs"] == []
                await assert_originals_unchanged("after_forget_student")
                return {"student_memory_version": snapshot["student_memory"]["version"], "empty_after_read": True}

            await check("MM03 撤销学生记忆后GET正文为空且不修改原始学习事实", forget_student)

            async def forget_experience():
                deletion = await request("DELETE", f"/sessions/{session_id}/memory/experience")
                assert deletion["session_experience"]["content"] == "" and deletion["session_experience"]["source_refs"] == []
                snapshot = await request("GET", f"/sessions/{session_id}/memory")
                report["final_memory"] = snapshot
                assert snapshot["student_memory"]["content"] == ""
                assert snapshot["session_experience"]["content"] == ""
                assert snapshot["session_experience"]["source_refs"] == []
                await assert_originals_unchanged("after_forget_experience")
                return {"experience_version": snapshot["session_experience"]["version"], "both_bodies_empty": True}

            await check("MM04 撤销学习经历后两类正文均为空且三类原始记录指纹不变", forget_experience)
            report["complete"] = True
        except Exception as exc:
            report["failure"] = {"type": type(exc).__name__, "detail": str(exc)[:1800]}
            print("FAIL " + type(exc).__name__ + ": " + str(exc)[:250], flush=True)
        finally:
            if report.get("session_id"):
                try:
                    report["final_audit"] = await asyncio.to_thread(read_audit, student_id, [report["session_id"]])
                    report["usage_totals"] = usage_totals(report["final_audit"])
                    report["original_final"] = await asyncio.to_thread(original_fingerprints, student_id, report["session_id"])
                except Exception as exc:
                    report["audit_failure"] = {"type": type(exc).__name__, "detail": str(exc)[:600]}
                    report["complete"] = False
            report["backend_source_sha256_after"] = source_digest()
            report["source_unchanged"] = report["backend_source_sha256_before"] == report["backend_source_sha256_after"]
            if not report["source_unchanged"]:
                report["complete"] = False
                report["source_change_failure"] = "验收期间后端源码发生变化，需要基于冻结版本复核。"
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            save()
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8011")
    parser.add_argument("--database", default="stem_tutor_memory_acceptance")
    parser.add_argument("--report", type=Path, default=ROOT / "data/reports/memory_maintenance_acceptance.json")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
