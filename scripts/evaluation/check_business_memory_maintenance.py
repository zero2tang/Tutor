"""复用合成学生补验BM07/BM08，明确与先前学习矩阵使用不同源码版本。"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from check_business_memory import (CORRECTION_A, EvaluationCheckFailed, EvaluationPaused,
    Harness, concept, ensure, semantic_scope)
from business_memory_audit import DATABASE, source_digest, usage_totals

ROOT = Path(__file__).resolve().parents[2]


def numeric_delta(before, after):
    return {key: round(value - before.get(key, 0), 3) for key, value in after.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)}


async def main(args):
    parsed = urlparse(args.base_url)
    ensure(parsed.hostname in {"localhost", "127.0.0.1"} and parsed.port == 8012, "只允许本机8012合成验收服务")
    ensure(args.database == DATABASE, "只允许独立业务记忆验收数据库")
    previous = json.loads(args.main_report.read_text(encoding="utf-8"))
    ensure(previous.get("database") == DATABASE and previous.get("student_id", "").startswith("evaluation_"), "不能对非合成学生执行补验")
    prior_passed = [row["label"].split()[0] for row in previous.get("checks", []) if row["status"] == "passed"]
    ensure({f"BM0{index}" for index in range(1, 7)} <= set(prior_passed), "先前报告未证明BM01-BM06主学习链成立")
    os.environ["TUTOR_DATABASE"] = args.database
    harness = Harness(args)
    harness.student = previous["student_id"]
    report = harness.report
    report["base_harness_script_sha256"] = report["script_sha256"]
    report["script_sha256"] = sha256(Path(__file__).read_bytes()).hexdigest()
    report.update(student_id=harness.student, sessions=previous["sessions"], catalog_fixture=previous["catalog_fixture"],
        report_purpose="targeted_BM07_BM08_after_course_projection_fix",
        previous_report=str(args.main_report.resolve()), previous_learning_hash=previous["backend_source_sha256_before"],
        previous_passed_checks=prior_passed, coverage_note="BM01-BM06仅在原hash实证；本报告只补验新hash下BM07/BM08，不能合称同hash全8通过。")
    report["execution"].update(database_write_scope="none; SELECT audit only", direct_database_writes=0)
    sid, physical_sid = report["sessions"]["first"], report["sessions"]["physics"]
    ids = report["catalog_fixture"]["concepts"]
    baseline = None
    try:
        await harness.request("GET", "/health")
        baseline = await harness.audit()
        ignored_failures = {row["id"] for row in baseline["scope_jobs"] if row["status"] == "failed"}
        report["retained_historical_failed_scope_job_ids"] = sorted(ignored_failures)
        report["historical_status_policy"] = "保留既有failed任务审计；只等待本次新事实引发的新代次，新失败仍阻止补验。"

        async def correct_course():
            before = await harness.audit()
            current = await harness.request("GET", f"/sessions/{sid}/memory")
            other_before = await harness.request("GET", f"/sessions/{physical_sid}/memory")
            before_refs = {row["source_ref"] for row in before["facts"]}
            response = await harness.request("POST", f"/sessions/{sid}/memory/correct?wait=true", {"kind": "course", "text": CORRECTION_A})
            report["course_correction_response"] = response
            snapshot = await harness.settle(sid, refresh=False, ignore_failed_scope_job_ids=ignored_failures)
            report["course_memory_after"] = snapshot
            after = await harness.audit()
            new_refs = {row["source_ref"] for row in after["facts"] if row["source_ref"] not in before_refs and row["source_ref"].startswith("correction:")}
            a = snapshot.get("course_memory")
            ensure(a and a.get("content") and "图" in a["content"], "新模型调用未重建课程纠正正文", snapshot)
            ensure(a["version"] > (current.get("course_memory") or {}).get("version", 0), "纠正未推进正文版本", a)
            ensure(new_refs.intersection(a["source_refs"]), "课程正文没有引用本次新纠正事实", {"new_refs": sorted(new_refs), "memory": a})
            ensure(before["originals"] == after["originals"], "课程纠正改写原始Attempt/State/正式归因或消息")
            other_after = await harness.request("GET", f"/sessions/{physical_sid}/memory")
            ensure(semantic_scope(other_before.get("course_memory")) == semantic_scope(other_after.get("course_memory")), "课程纠正误伤另一门课偏好")
            return {"new_correction_refs": sorted(new_refs), "course_memory": a, "originals_unchanged": after["originals"], "unrelated_course_preserved": True}

        await harness.check("BM07 课程偏好纠正真实重建且原始评估与其它课程保持", correct_course)

        async def scoped_forget():
            before = await harness.audit()
            memory = await harness.request("GET", f"/sessions/{sid}/memory")
            target, unrelated = concept(memory, ids["distribution"]), concept(memory, ids["equality"])
            ensure(target and target.get("content"), "目标知识点没有可遗忘正文", target)
            physical_before = await harness.request("GET", f"/sessions/{physical_sid}/memory")
            await harness.request("DELETE", f"/sessions/{sid}/memory/concept?concept_id={ids['distribution']}")
            immediate = await harness.request("GET", f"/sessions/{sid}/memory")
            ensure(not (concept(immediate, ids["distribution"]) or {}).get("content"), "遗忘后仍返回目标旧正文")
            ensure(semantic_scope(unrelated) == semantic_scope(concept(immediate, ids["equality"])), "概念遗忘误伤等式性质记忆")
            physical_after = await harness.request("GET", f"/sessions/{physical_sid}/memory")
            ensure(semantic_scope(physical_before.get("course_memory")) == semantic_scope(physical_after.get("course_memory")), "概念遗忘误伤物理课记忆")
            after = await harness.audit()
            ensure(before["originals"] == after["originals"], "遗忘摘要修改原始作答或正式掌握度")
            observations = [row for row in after["observations"] if row["concept_id"] == ids["distribution"]]
            ensure(observations and all(row["status"] != "active" for row in observations), "旧会话概念观察未失效", observations)
            scopes = {row["scope_key"]: row for row in after["scopes"]}
            parents = {row["parent_scope_key"] for row in before["dependencies"] if row["child_kind"] == "scope"
                and row["child_key"] == target["scope_key"] and scopes.get(row["parent_scope_key"], {}).get("version") == row["parent_version"]}
            ensure(all(scopes[key]["status"] == "stale" for key in parents), "实际依赖目标概念的上层仍可读", sorted(parents))
            snapshot = await harness.settle(sid, ignore_failed_scope_job_ids=ignored_failures)
            ensure(not (concept(snapshot, ids["distribution"]) or {}).get("content"), "刷新复活了旧概念正文")
            final = await harness.audit()
            ensure(after["originals"] == final["originals"], "遗忘后刷新更改正式证据或状态")
            return {"forgotten_scope_key": target["scope_key"], "invalidated_observations": [row["id"] for row in observations],
                "actual_dependent_parent_scopes": sorted(parents), "originals_unchanged": final["originals"], "unrelated_scope_preserved": True}

        await harness.check("BM08 概念遗忘传播且旧来源不复活，正式证据与无关作用域保持", scoped_forget)
        report["complete"] = len(report["checks"]) == 2 and all(row["status"] == "passed" for row in report["checks"])
    except EvaluationCheckFailed as exc:
        report.update(stopped_on_failure=True, failed_check=str(exc))
    except EvaluationPaused as exc:
        report.update(interrupted=True, interruption_reason=str(exc))
    except Exception as exc:
        report["failure"] = {"type": type(exc).__name__, "message": str(exc)[:2400]}
    finally:
        try:
            report["audit"] = await harness.audit()
            if baseline is not None:
                report["usage_delta_for_this_branch"] = numeric_delta(usage_totals(baseline), usage_totals(report["audit"]))
                report["new_leaf_jobs"] = [row for row in report["audit"]["jobs"] if row["id"] not in {item["id"] for item in baseline["jobs"]}]
                report["new_scope_jobs"] = [row for row in report["audit"]["scope_jobs"] if row["id"] not in {item["id"] for item in baseline["scope_jobs"]}]
        except Exception as exc:
            report.update(audit_error=str(exc)[:1200], complete=False)
        report["backend_source_sha256_after"] = source_digest()
        report["source_unchanged_during_run"] = report["backend_source_sha256_before"] == report["backend_source_sha256_after"]
        report["complete"] = report["complete"] and report["source_unchanged_during_run"]
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        harness.save()
        await harness.client.aclose()
    print(json.dumps({"complete": report["complete"], "report": str(args.report), "checks": report["checks"]}, ensure_ascii=True), flush=True)
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8012")
    parser.add_argument("--database", default=DATABASE)
    parser.add_argument("--main-report", type=Path, default=ROOT / "data/reports/business_memory_acceptance_before_course_candidate_fix.json")
    parser.add_argument("--report", type=Path, default=ROOT / "data/reports/business_memory_maintenance_acceptance.json")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
