"""独立真实验收知识点纠正：只新增明确自述事实，不制造正式作答。"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from check_business_memory import Harness, concept, ensure, semantic_scope
from business_memory_audit import DATABASE, source_digest, usage_totals

ROOT = Path(__file__).resolve().parents[2]
CORRECTION = (
    "这是脚本模拟学生对分配律知识点记忆的明确纠正：我并未声称自己已经稳定掌握。"
    "请记录为我自己的报告：在含常数项的括号展开时，我会特别检查系数是否也乘到常数。"
    "这条仅是我自述的学习策略，不是新的正式作答或评分结论；不要改已有作答、分数或掌握状态，"
    "也不要扩展到其它知识点或课程。"
)


def numeric_delta(before, after):
    return {key: round(value - before.get(key, 0), 3) for key, value in after.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)}


async def main(args):
    parsed = urlparse(args.base_url)
    ensure(parsed.hostname in {"127.0.0.1", "localhost"} and parsed.port == 8012,
        "只允许本机8012合成验收服务")
    ensure(args.database == DATABASE, "只允许独立业务记忆验收数据库")
    previous = json.loads(args.main_report.read_text(encoding="utf-8"))
    ensure(previous.get("database") == DATABASE and previous.get("student_id", "").startswith("evaluation_"),
        "主报告不是合成验收学生，拒绝操作")
    ensure({"first", "physics"} <= set(previous.get("sessions", {})), "主报告缺少已建立的代数与物理会话")
    os.environ["TUTOR_DATABASE"] = args.database
    harness = Harness(args)
    harness.student = previous["student_id"]
    report = harness.report
    report["base_harness_script_sha256"] = report["script_sha256"]
    report["script_sha256"] = sha256(Path(__file__).read_bytes()).hexdigest()
    report["execution"].update(database_write_scope="none; SELECT audit only", direct_database_writes=0)
    report.update(student_id=harness.student, sessions=previous["sessions"],
        main_report=str(args.main_report.resolve()), report_purpose="concept_correction_only_independent_branch",
        student_text=CORRECTION, data_origin="explicit_synthetic_student_self_report_correction; reuse_main_acceptance_owner")
    sid, other_sid = previous["sessions"]["first"], previous["sessions"]["physics"]
    cid = previous["catalog_fixture"]["concepts"]["distribution"]
    unrelated_cid = previous["catalog_fixture"]["concepts"]["equality"]
    before = None
    try:
        await harness.request("GET", "/health")
        await harness.settle(sid, refresh=False)
        before = await harness.audit()
        report["original_baseline"] = before["originals"]
        before_memory = await harness.request("GET", f"/sessions/{sid}/memory")
        other_before = await harness.request("GET", f"/sessions/{other_sid}/memory")
        before_refs = {row["source_ref"] for row in before["facts"]}
        response = await harness.request("POST", f"/sessions/{sid}/memory/correct?wait=true",
            {"kind": "concept", "concept_id": cid, "text": CORRECTION})
        report["correction_response"] = response
        snapshot = await harness.settle(sid, refresh=False)
        report["memory_after"] = snapshot
        after = await harness.audit()
        report["audit"] = after
        new_corrections = {row["source_ref"] for row in after["facts"] if row["source_ref"] not in before_refs
            and row["source_ref"].startswith("correction:")}
        target = concept(snapshot, cid)
        ensure(target and target.get("content"), "知识点纠正没有产生模型记忆正文", snapshot)
        claims = target.get("claims", [])
        supported = [claim for claim in claims if new_corrections.intersection(
            [*claim.get("source_refs", []), *claim.get("counter_source_refs", [])])]
        ensure(supported, "知识点纠正正文未引用本次明确纠正来源", {"new_corrections": sorted(new_corrections), "memory": target})
        ensure(any(claim.get("source_kind") == "self_report" for claim in supported),
            "学生自述被错误升级为正式判分或客观掌握结论", supported)
        ensure(all(claim.get("source_kind") != "graded_attempt" for claim in supported),
            "纠正文字不能生成新的正式评估条目", supported)
        ensure(before["originals"] == after["originals"], "知识点纠正改写Attempt/StudentState/正式归因或原消息",
            {"before": before["originals"], "after": after["originals"]})
        ensure(semantic_scope(concept(before_memory, unrelated_cid)) == semantic_scope(concept(snapshot, unrelated_cid)),
            "知识点纠正误伤其它知识点记忆")
        other_after = await harness.request("GET", f"/sessions/{other_sid}/memory")
        ensure(semantic_scope(other_before.get("course_memory")) == semantic_scope(other_after.get("course_memory")),
            "知识点纠正改写了另一门课的偏好")
        delta = numeric_delta(usage_totals(before), usage_totals(after))
        ensure(delta.get("leaf_memory_model_calls", 0) > 0, "没有本分支真实叶子模型提取用量", delta)
        report.update(checks=[{"label": "CC01 知识点纠正保持明确自述来源且正式证据与无关作用域不变", "status": "passed",
            "details": {"concept_id": cid, "new_correction_sources": sorted(new_corrections), "supported_claims": supported,
                "originals_unchanged": after["originals"]}}], usage_delta_for_this_branch=delta, complete=True)
    except Exception as exc:
        report["failure"] = {"type": type(exc).__name__, "message": str(exc)[:2400]}
        report["checks"].append({"label": "CC01 知识点纠正保持明确自述来源且正式证据与无关作用域不变",
            "status": "failed", "detail": str(exc)[:2400]})
    finally:
        try:
            report["audit"] = await harness.audit()
            if before is not None:
                report["usage_delta_for_this_branch"] = numeric_delta(usage_totals(before), usage_totals(report["audit"]))
                report["new_leaf_jobs"] = [row for row in report["audit"]["jobs"] if row["id"] not in {item["id"] for item in before["jobs"]}]
                report["new_scope_jobs"] = [row for row in report["audit"]["scope_jobs"] if row["id"] not in {item["id"] for item in before["scope_jobs"]}]
        except Exception as exc:
            report["audit_error"] = str(exc)[:1200]
            report["complete"] = False
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
    parser.add_argument("--main-report", type=Path, default=ROOT / "data/reports/business_memory_acceptance.json")
    parser.add_argument("--report", type=Path, default=ROOT / "data/reports/business_concept_correction_acceptance.json")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
