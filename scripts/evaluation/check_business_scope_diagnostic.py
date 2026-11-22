"""只读恢复合成验收失败输入；可选单次真实模型请求，禁止写学习数据库。"""

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))


def snapshot(args):
    from sqlalchemy import select
    from app.infrastructure.config import get_settings
    from app.infrastructure.persistence.database import db_session
    from app.domains.memory.scopes.dependencies import allowed_sources, source_facts
    from app.domains.memory.scopes.service import HierarchicalMemoryService, input_fingerprint
    from app.domains.memory.scopes.preferences import preference_index, valid_upper_claims
    from app.domains.memory.models import MemoryJob
    from app.domains.memory.scopes.models import ScopeMemory
    from app.domains.sessions.models import LearningSession, Message

    report = json.loads(args.evidence_report.read_text(encoding="utf-8"))
    assert "disclosed_synthetic_catalog_and_scripted_student_responses" in report["data_origin"]
    assert get_settings().database_name == "stem_tutor_business_memory_acceptance"
    with db_session() as db:
        job = db.get(MemoryJob, args.job_id)
        row = db.get(ScopeMemory, job.scope_key)
        assert row.student_id == report["student_id"] and row.student_id.startswith("evaluation_")
        assert row.course_id.startswith("bm_alg_") and row.scope_kind == "course"
        state = db.get(ScopeMemory, job.scope_key)
        messages = list(db.scalars(select(Message).join(LearningSession, LearningSession.id == Message.session_id)
            .where(LearningSession.student_id == row.student_id, Message.role == "user")))
        assert messages and all("合成工程验收" in m.content or "脚本模拟作答" in m.content for m in messages)
        service = HierarchicalMemoryService(None)
        children, preferences, extra = service._inputs(db, row.student_id, row.scope_kind, row.course_id)
        if input_fingerprint(children + extra) != job.input_fingerprint:
            for child in children:
                historical = db.scalar(select(MemoryJob).where(MemoryJob.scope_key == child["key"],
                    MemoryJob.status == "completed", MemoryJob.created_at <= job.created_at)
                    .order_by(MemoryJob.created_at.desc()).limit(1))
                assert historical and historical.source_generation == 1, "仅恢复有完整首次概念聚合审计的失败输入"
                child.update(version=1, claims=historical.result_snapshot["claims"],
                             source_refs=historical.result_snapshot["source_refs"])
        assert input_fingerprint(children + extra) == job.input_fingerprint, "无法恢复相同来源版本指纹"
        selected, preferences = children[:8], preferences[:8]
        old_claims = valid_upper_claims(row)
        refs = {ref for child in selected for ref in child["source_refs"]}
        refs.update(ref for p in preferences for ref in p.get("source_refs", []))
        refs.update(ref for c in old_claims for ref in c.get("source_refs", []) + c.get("counter_source_refs", []))
        facts = source_facts(db, row.student_id, allowed_sources(db, state, refs), row.course_id)
        preferences = [p for p in preferences if set(p.get("source_refs", [])) <= set(facts)]
        for child in selected:
            for claim in child["claims"]:
                for ref in claim.get("source_refs", []):
                    if ref in facts:
                        facts[ref]["concept_ids"] = list(set(facts[ref].get("concept_ids", [])) |
                            set(claim.get("concept_ids", [])) | ({child["concept_id"]} if child["concept_id"] else set()))
        behavioral = {ref for child in selected for claim in child["claims"] if not claim.get("explicit_preference")
                      for ref in claim.get("source_refs", [])}
        for ref, fact in facts.items():
            fact["preference_only"] = ref not in behavioral
        pref_index = preference_index(preferences, row.scope_kind)
        old_claims = [c for c in old_claims if set(c.get("source_refs", []) + c.get("counter_source_refs", [])) <= set(facts)
            and (not c.get("explicit_preference") or c.get("preference_id") in pref_index)]
        for fact in facts.values():
            payload = fact.pop("payload")
            quotes = [q for p in preferences for q in p.get("source_quotes", []) if q.get("source_ref") == fact["source_ref"]]
            raw = json.dumps({"scope_kind": row.scope_kind, "authorized_quotes": quotes}
                if fact.get("preference_only") else payload, ensure_ascii=False)
            fact["source_excerpt"] = {"text": raw[:600], "start": 0, "end": min(600, len(raw)),
                                      "total_chars": len(raw), "partial": len(raw) > 600}
        model_input = {"scope_kind": row.scope_kind, "course_id": row.course_id, "old_claims": old_claims,
            "children": [{"course_id": c["course_id"], "concept_id": c["concept_id"], "claims": c["claims"][:3]}
                         for c in selected], "preferences": preferences, "facts": facts}
        proof = {"database_whitelist": get_settings().database_name, "student_id": row.student_id,
            "course_id": row.course_id, "job_id": job.id, "failed_input_fingerprint": job.input_fingerprint,
            "matched_source_versions": True, "data_origin": report["data_origin"],
            "all_user_messages_explicitly_scripted": True, "user_message_count": len(messages),
            "source_fact_refs": sorted(facts), "user_message_sha256": [sha256(m.content.encode()).hexdigest() for m in messages],
            "source_scope": "only the named synthetic student and synthetic algebra course",
            "database_writes": 0, "credentials_or_provider_address_persisted": False}
    return model_input, proof


class ReviewNotExecuted(RuntimeError):
    pass


async def main(args):
    os.environ["TUTOR_DATABASE"] = "stem_tutor_business_memory_acceptance"
    model_input, proof = snapshot(args)
    report = {"at": datetime.now(timezone.utc).isoformat(), "proof": proof, "actual_model_calls": 0,
              "execute": args.execute, "model_input": model_input}
    if args.execute:
        from app.infrastructure.clients.llm import ModelClient
        from app.domains.memory.scopes.promotion import aggregate_candidate, empty_metrics

        model = ModelClient()

        from unittest.mock import patch
        from app.infrastructure.clients.chat import invoke_structured

        async def single_call(models, messages, schema, **kwargs):
            if report["actual_model_calls"]:
                raise ReviewNotExecuted("候选已通过聚合校验，独立review不在本次单请求诊断范围")
            report["actual_model_calls"] += 1
            response = await invoke_structured(models, messages, schema, **kwargs)
            report["candidate"] = response["raw"].tool_calls
            report["usage"] = response["raw"].usage_metadata
            return response
        try:
            with patch("app.domains.memory.scopes.promotion.invoke_structured", single_call):
                claims, audit = await aggregate_candidate(model, model_input, empty_metrics())
            report.update(validation="passed", claims=claims, audit=audit)
        except Exception as exc:
            report.update(validation="passed_requires_review" if isinstance(exc, ReviewNotExecuted) else "failed",
                          error={"type": type(exc).__name__, "message": str(exc)})
        finally:
            await model.http.aclose()
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"proof": proof, "actual_model_calls": report["actual_model_calls"],
        "validation": report.get("validation"), "error": report.get("error"), "candidate": report.get("candidate"),
        "usage": report.get("usage"), "report": str(args.report.resolve())}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", default="c19d8318-4689-44a3-a155-6b2bc1b661f1")
    parser.add_argument("--evidence-report", type=Path,
        default=ROOT / "data/reports/business_memory_acceptance_before_upper_id_fix.json")
    parser.add_argument("--report", type=Path, default=ROOT / "data/reports/business_scope_failure_diagnostic.json")
    parser.add_argument("--execute", action="store_true")
    asyncio.run(main(parser.parse_args()))
