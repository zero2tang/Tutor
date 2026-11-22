"""P0 一次性归档和核验；仅显式指定的五张退役表可以被删除。"""

import argparse
import base64
from datetime import datetime
from decimal import Decimal
import gzip
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import MetaData, Table, inspect, select, text
from app.infrastructure.persistence.database import engine

TABLES = ("textbook_extracted_resources", "exercise_search_methods", "run_events",
          "tutoring_actions", "tutoring_evidence")
ARCHIVE = ROOT / "data/archives/simplification_p0/20261023"
REPORT = ROOT / "data/reports/simplification_p0"


def encode(value):
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode()}
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    raise TypeError(type(value).__name__)


def decode(value):
    if set(value) == {"$datetime"}:
        return datetime.fromisoformat(value["$datetime"])
    if set(value) == {"$bytes"}:
        return base64.b64decode(value["$bytes"])
    if set(value) == {"$decimal"}:
        return Decimal(value["$decimal"])
    return value


def serialized(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=encode)


def digest(value):
    return hashlib.sha256(serialized(value).encode()).hexdigest()


def rows(connection, name):
    table = Table(name, MetaData(), autoload_with=connection)
    return table, [dict(row) for row in connection.execute(
        select(table).order_by(*table.primary_key.columns)).mappings()]


def source_manifest():
    paths = list((ROOT / "backend/app").rglob("*.py"))
    paths += list((ROOT / "frontend/src").rglob("*"))
    return {p.relative_to(ROOT).as_posix(): {
        "lines": len(p.read_text(encoding="utf-8").splitlines()),
        "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
        for p in sorted(paths) if p.is_file() and "__pycache__" not in p.parts}


def prepare():
    if (ARCHIVE / "retired.json.gz").exists():
        raise RuntimeError("基线已存在，禁止覆盖；请使用 verify 核对。")
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    REPORT.mkdir(parents=True, exist_ok=True)
    snapshot = {"schema_version": 1, "created_at": datetime.now().isoformat(), "tables": {}}
    baseline = {"source": source_manifest(), "tables": {}}
    with engine().connect() as c:
        assert not c.execute(text("SELECT id FROM learning_sessions WHERE active_turn_id IS NOT NULL")).first(), "仍有在途会话"
        for name in inspect(c).get_table_names():
            table, data = rows(c, name)
            baseline["tables"][name] = {"count": len(data), "sha256": digest(data)}
            if name in TABLES:
                ddl = c.execute(text(f"SHOW CREATE TABLE `{name}`")).one()[1]
                snapshot["tables"][name] = {"ddl": ddl, "rows": data, "sha256": digest(data)}
        # 状态转换涉及的表另外完整备份，保留原始会话和撤回屏障。
        for name in ("learning_sessions", "memory_facts", "memory_session_states"):
            _, data = rows(c, name)
            snapshot[name] = data
    raw = serialized(snapshot).encode()
    (ARCHIVE / "retired.json.gz").write_bytes(gzip.compress(raw))
    baseline["archive_sha256"] = hashlib.sha256(raw).hexdigest()
    (REPORT / "before.json").write_text(serialized(baseline), encoding="utf-8")
    with zipfile.ZipFile(ARCHIVE / "source_before.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for name in baseline["source"]:
            z.write(ROOT / name, name)
    print(serialized({"tables": len(baseline["tables"]), "archived": {
        n: len(t["rows"]) for n, t in snapshot["tables"].items()}}))


def load():
    raw = gzip.decompress((ARCHIVE / "retired.json.gz").read_bytes())
    baseline = json.loads((REPORT / "before.json").read_text(encoding="utf-8"))
    assert hashlib.sha256(raw).hexdigest() == baseline["archive_sha256"]
    return json.loads(raw, object_hook=decode), baseline


def verify():
    snapshot, baseline = load()
    result = {"archive_valid": True, "roundtrip": {}, "tables": {}}
    with engine().begin() as c:
        for name, archived in snapshot["tables"].items():
            assert digest(archived["rows"]) == archived["sha256"]
            # 临时表验证完整 DDL 和每条数据确实可还原，不产生永久业务表。
            probe = "p0_restore_" + name
            ddl = archived["ddl"].replace(f"CREATE TABLE `{name}`", f"CREATE TEMPORARY TABLE `{probe}`", 1)
            c.execute(text(ddl))
            target = Table(probe, MetaData(), autoload_with=c)
            data = archived["rows"]
            for start in range(0, len(data), 100):
                c.execute(target.insert(), data[start:start + 100])
            restored = [dict(r) for r in c.execute(select(target).order_by(*target.primary_key.columns)).mappings()]
            assert digest(restored) == archived["sha256"], name
            c.execute(text(f"DROP TEMPORARY TABLE `{probe}`"))
            result["roundtrip"][name] = len(restored)
        for name in inspect(c).get_table_names():
            _, data = rows(c, name)
            result["tables"][name] = {"count": len(data), "sha256": digest(data)}
    (REPORT / "archive_verification.json").write_text(serialized(result), encoding="utf-8")
    print(serialized({"archive_valid": True, "roundtrip": result["roundtrip"]}))


def migrate():
    """先生成历史快照和原始观察事实，再允许移除旧读取。"""
    snapshot, baseline = load()
    from app.infrastructure.persistence.database import db_session
    from app.domains.sessions.models import LearningSession
    from app.domains.memory.recording.facts import record_fact
    from app.domains.memory.models import MemoryFact
    from app.domains.materials.extraction.models import ExtractedSection
    from app.infrastructure.documents.pdf import digest as payload_digest

    with db_session() as db:
        # 逐资源核对三类内容和教学模式都存在于小节原始 payload。
        for item in snapshot["tables"]["textbook_extracted_resources"]["rows"]:
            section = db.get(ExtractedSection, item["section_id"])
            source = section.payload["teaching_design"] if item["kind"] == "teaching" else next(
                v for v in section.payload[item["kind"]] if item["id"] == f"{section.id}:{v['source_id']}")
            assert payload_digest(source) == item["payload_hash"] == payload_digest(item["payload"])
    students = sorted({r["student_id"] for r in snapshot["tables"]["tutoring_evidence"]["rows"]})
    summaries = {s: {**historical_summary([r for r in snapshot["tables"]["tutoring_evidence"]["rows"] if r["student_id"] == s]), "archive_version": 1,
        "archive_created_at": snapshot["created_at"]} for s in students}
    path = ARCHIVE / "student_summaries.json"
    path.write_text(serialized({"version": 1, "students": summaries}), encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8"))["students"] == summaries
    converted, inserted, archived_only = [], 0, 0
    with db_session() as db:
        sessions = {r.id: r for r in db.scalars(select(LearningSession))}
        assert not any(r.active_turn_id for r in sessions.values()), "仍有在途会话"
        for evidence in snapshot["tables"]["tutoring_evidence"]["rows"]:
            session = sessions.get(evidence["session_id"])
            if session is None or session.student_id != evidence["student_id"]:
                archived_only += 1
                continue
            ref = "tutoring_evidence:" + evidence["id"]
            old = db.scalar(select(MemoryFact).where(MemoryFact.session_id == session.id, MemoryFact.source_ref == ref))
            if old is None:
                record_fact(db, session.id, ref, "tutoring_evidence", {
                    **{key: evidence[key] for key in ("question_id", "question_hash", "kind", "independent", "simulated", "details")},
                    "_source_created_at": evidence["created_at"].isoformat()}, reopen=False)
                inserted += 1
        for row in sessions.values():
            context = dict(row.pending_context or {})
            legacy = context.pop("equation_tutoring", None)
            if legacy:
                contract = legacy.get("contract") or {}
                context["archived_problem"] = {"question_id": contract.get("id"),
                    "stem": contract.get("original_equation") or contract.get("equation") or "历史习题",
                    "status": "archived", "notice": "原题和历史记录已归档，可通过通用讲题继续学习。"}
                row.pending_context = context
                row.version += 1
                converted.append(row.id)
    report = {"section_resources_verified": len(snapshot["tables"]["textbook_extracted_resources"]["rows"]),
        "frozen_student_summaries": len(students), "memory_facts_inserted": inserted,
        "archived_only_evidence": archived_only, "converted_sessions": converted}
    (REPORT / "migration.json").write_text(serialized(report), encoding="utf-8")
    print(serialized(report))


def historical_summary(data):
    """一次性计算固定历史展示，不再在在线档案请求里运行。"""
    data = sorted(data, key=lambda r: (r["created_at"], r["id"]))
    disputed = {r["question_id"] for r in data if r["kind"] == "dispute"}
    completed = [r for r in data if r["kind"] == "completion" and r["question_id"] not in disputed]
    independent = [r for r in completed if r["independent"] and r["details"].get("stage") == "transfer"]
    return {"status": "independently_demonstrated" if independent else "practicing" if data else "unassessed",
        "independent_transfer_count": len({r["question_id"] for r in independent}),
        "assisted_completion_count": sum(bool(r["details"].get("assisted")) for r in completed),
        "observed_steps": sum(r["kind"] in {"step", "completion"} for r in data),
        "pending_review_count": len(disputed), "mastery_probability": None, "legacy": True,
        "note": "仅汇总退役流程的历史观察，不追加掌握证据；尚未验证长期保持，也未校准掌握概率。",
        "evidence": [{"id": r["id"], "question_id": r["question_id"], "independent": r["independent"],
            "assisted": bool(r["details"].get("assisted")), "stage": r["details"].get("stage"),
            "simulated": r["simulated"], "scope": (r["details"].get("verdict") or {}).get("completion", {}).get("evidence_scope")}
            for r in completed[-12:]]}


def retire():
    """新代码不再登记这些模型，且归档一致时，才物理退表。"""
    from app.application.model_registry import register_models
    metadata = register_models()
    assert not set(TABLES).intersection(metadata.tables), "启动仍可能重建退役表"
    snapshot, baseline = load()
    assert (REPORT / "archive_verification.json").exists()
    assert (ARCHIVE / "student_summaries.json").exists()
    with engine().begin() as c:
        assert not c.execute(text("SELECT id FROM learning_sessions WHERE active_turn_id IS NOT NULL")).first()
        existing = set(inspect(c).get_table_names())
        # 全部前置检查在任何 DROP 之前执行，避免发现新增数据时只退了一半。
        for name in TABLES:
            if name in existing:
                _, data = rows(c, name)
                assert digest(data) == snapshot["tables"][name]["sha256"], f"归档后数据变化: {name}"
        for name in TABLES:
            if name in existing:
                c.execute(text(f"DROP TABLE `{name}`"))
    print(serialized({"retired": list(TABLES), "running_tables": len(inspect(engine()).get_table_names())}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "verify", "migrate", "retire"])
    args = parser.parse_args()
    {"prepare": prepare, "verify": verify, "migrate": migrate, "retire": retire}[args.action]()
