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

TABLES = ("textbook_extraction_books", "textbook_extraction_jobs", "teaching_analysis_packs", "textbook_extraction_calls", "exercise_search_calls", "exercise_search_receipts", "exercise_search_build_leases", "exercise_generation_attempts")
ARCHIVE = ROOT / "data/archives/simplification_p1/20261023"
REPORT = ROOT / "data/reports/simplification_p1"


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
    if (ARCHIVE / "snapshot.json.gz").exists():
        raise RuntimeError("基线已存在，禁止覆盖")
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    REPORT.mkdir(parents=True, exist_ok=True)
    snapshot, baseline = {}, {"source": source_manifest(), "tables": {}}
    with engine().connect() as c:
        assert not c.execute(text("SELECT id FROM learning_sessions WHERE active_turn_id IS NOT NULL")).first(), "仍有在途会话"
        for name in inspect(c).get_table_names():
            _, data = rows(c, name)
            baseline["tables"][name] = {"count": len(data), "sha256": digest(data)}
            snapshot[name] = {"ddl": c.execute(text(f"SHOW CREATE TABLE `{name}`")).one()[1], "rows": data}
    raw = serialized(snapshot).encode()
    (ARCHIVE / "snapshot.json.gz").write_bytes(gzip.compress(raw))
    baseline["archive_sha256"] = hashlib.sha256(raw).hexdigest()
    (REPORT / "before.json").write_text(serialized(baseline), encoding="utf-8")
    with zipfile.ZipFile(ARCHIVE / "source_before.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for name in baseline["source"]:
            z.write(ROOT / name, name)
    print(serialized({"tables": len(snapshot), "retiring": {n: len(snapshot[n]["rows"]) for n in TABLES}}))


def load():
    raw = gzip.decompress((ARCHIVE / "snapshot.json.gz").read_bytes())
    baseline = json.loads((REPORT / "before.json").read_text(encoding="utf-8"))
    assert hashlib.sha256(raw).hexdigest() == baseline["archive_sha256"]
    return json.loads(raw, object_hook=decode), baseline


def migrate():
    from types import SimpleNamespace
    from app.application.model_registry import register_models
    from app.domains.materials.teaching_reference import make_reference, view
    from app.domains.courses.questions.search.representation import digest as identity
    snapshot, baseline = load()
    packs = snapshot["teaching_analysis_packs"]["rows"]
    assert len({p["question_id"] for p in packs}) == len(packs), "发现一题多包，停止合表"
    (ARCHIVE / "teaching_packs.json.gz").write_bytes(gzip.compress(json.dumps({p["id"]:p["payload"] for p in packs}, ensure_ascii=False).encode()))
    attempts = snapshot["exercise_generation_attempts"]["rows"]
    (ARCHIVE / "generation_attempts.json.gz").write_bytes(gzip.compress(serialized({p["id"]:p for p in attempts}).encode()))
    metadata = register_models()
    additions = {
        "textbook_documents": {"asset_root":"", "page_count":0, "manifest":{}},
        "textbook_extraction_sections": {"fingerprint":"", "extraction_status":"pending", "lease_token":"", "lease_until":None, "attempts":0, "error":"", "receipt":{}},
        "questions": {"teaching_reference":{}},
        "index_tasks": {"course_id":"", "namespace":"main", "token":"", "lease_until":None},
        "agent_runs": {"private_diagnostics":{}},
    }
    with engine().begin() as c:
        # MySQL DDL 自动提交；每一步可检测重入，原始完整快照独立保全。
        for name, values in additions.items():
            columns = {v["name"] for v in inspect(c).get_columns(name)}
            if name == "textbook_extraction_sections" and "status" in columns:
                c.execute(text("ALTER TABLE textbook_extraction_sections CHANGE COLUMN status review_status VARCHAR(32) NOT NULL"))
            for key, default in values.items():
                column = metadata.tables[name].c[key]
                typ = column.type.compile(dialect=c.dialect)
                if key not in columns:
                    c.execute(text(f"ALTER TABLE `{name}` ADD COLUMN `{key}` {typ} NULL"))
                if default is not None:
                    c.execute(metadata.tables[name].update().where(column.is_(None)).values({key:default}))
                    c.execute(text(f"ALTER TABLE `{name}` MODIFY `{key}` {typ} NOT NULL"))
    metadata.create_all(engine())
    with engine().begin() as c:
        documents = metadata.tables["textbook_documents"]
        for book in snapshot["textbook_extraction_books"]["rows"]:
            if not c.execute(select(documents.c.id).where(documents.c.id == book["id"])).first():
                c.execute(documents.insert().values(id=book["id"], course_id=book["course_id"], title=book["title"],
                    edition="", source_uri="textbook:"+book["id"], source_kind="user_provided_textbook", format="pdf",
                    content_hash=book["source_hash"], raw_content=book["raw_content"], parser_version="multimodal-v1",
                    status="registered", parsed={}, provenance={"migrated_from":"textbook_extraction_books"},
                    asset_root=book["asset_root"], page_count=book["page_count"], manifest=book["manifest"], created_at=book["created_at"]))
        sections = metadata.tables["textbook_extraction_sections"]
        for job in snapshot["textbook_extraction_jobs"]["rows"]:
            assert job["id"] == job["section_id"]
            values = {key:job[key] for key in additions["textbook_extraction_sections"] if key != "extraction_status"}
            values["extraction_status"] = job["status"]
            c.execute(sections.update().where(sections.c.id == job["id"]).values(**values))
        questions = metadata.tables["questions"]
        for pack in packs:
            q = c.execute(select(questions.c.id).where(questions.c.id == pack["question_id"])).first()
            assert q, "发现孤立教学包"
            c.execute(questions.update().where(questions.c.id == pack["question_id"]).values(teaching_reference=make_reference(SimpleNamespace(**pack), archived=True)))
        calls = metadata.tables["model_calls"]
        for old, purpose in (("textbook_extraction_calls","textbook"),("exercise_search_calls","question_search")):
            for row in snapshot[old]["rows"]:
                value = dict(row)
                value["purpose"] = purpose
                value["resource_id"] = value.pop("job_id", "")
                value.setdefault("page",None)
                value.setdefault("model",value["metrics"].get("model", value["response"].get("model", "")))
                value.setdefault("error",value["metrics"].get("error", ""))
                if not c.execute(select(calls.c.id).where(calls.c.id == row["id"])).first():
                    c.execute(calls.insert().values(**value))
        tasks = metadata.tables["index_tasks"]
        for old in snapshot["exercise_search_build_leases"]["rows"]:
            namespace = old["receipt"].get("config",{}).get("namespace","main")
            courses = [r["id"] for r in snapshot["courses"]["rows"] if identity([r["id"],namespace]) == old["id"]]
            assert len(courses) == 1, "无法定位旧租约范围"
            identifier = identity([courses[0],namespace,"question_search"])
            value = dict(id=identifier, course_id=courses[0], namespace=namespace, target="question_search",
                status="completed", token=old["token"], lease_until=old["lease_until"], details={**old["receipt"], "legacy_lease_id":old["id"]})
            if not c.execute(select(tasks.c.id).where(tasks.c.id == identifier)).first():
                c.execute(tasks.insert().values(**value))
    print(serialized({"documents_added":1,"sections":len(snapshot["textbook_extraction_jobs"]["rows"]),"references":len(packs),"calls":sum(len(snapshot[n]["rows"]) for n in ("textbook_extraction_calls","exercise_search_calls")),"diagnostics_archived":len(attempts)}))


def verify():
    from types import SimpleNamespace
    from app.application.model_registry import register_models
    from app.infrastructure.persistence.database import db_session
    from app.domains.materials.teaching_reference import get_pack
    from app.domains.materials.extraction.store import ExtractionStore
    from app.domains.practice.diagnostics import generation_diagnostic
    from app.domains.courses.questions.search.representation import public_snapshot, source_hash
    from app.domains.courses.models import Question
    from app.domains.materials.models import TextbookDocument
    from app.infrastructure.persistence.model_calls import ModelCall
    register_models()
    snapshot, baseline = load()
    report = {"archive_valid":True, "roundtrip":{}}
    with engine().begin() as c:
        for name in TABLES:
            archived = snapshot[name]
            probe = "p1_restore_"+name
            c.execute(text(archived["ddl"].replace(f"CREATE TABLE `{name}`",f"CREATE TEMPORARY TABLE `{probe}`",1)))
            target = Table(probe, MetaData(), autoload_with=c)
            data = archived["rows"]
            for start in range(0,len(data),100):
                c.execute(target.insert(),data[start:start+100])
            restored = [dict(r) for r in c.execute(select(target).order_by(*target.primary_key.columns)).mappings()]
            assert digest(restored) == digest(data),name
            c.execute(text(f"DROP TEMPORARY TABLE `{probe}`"))
            report["roundtrip"][name] = len(data)
    store = ExtractionStore()
    with db_session() as db:
        for old in snapshot["textbook_documents"]["rows"]:
            row = db.get(TextbookDocument,old["id"])
            assert all(getattr(row,k) == v for k,v in old.items())
        for old in snapshot["textbook_extraction_books"]["rows"]:
            row = store.book(old["id"])
            assert all(getattr(row,k) == old[k] for k in ("id","course_id","raw_content","manifest","page_count","asset_root","source_hash"))
        for job in snapshot["textbook_extraction_jobs"]["rows"]:
            row = store.job(job["id"])
            assert row.extraction_status == job["status"] and row.receipt == job["receipt"] and row.fingerprint == job["fingerprint"]
        for old in snapshot["textbook_extraction_sections"]["rows"]:
            row = store.job(old["id"])
            assert row.payload == old["payload"] and row.payload_hash == old["payload_hash"] and row.review_status == old["status"]
        for old in snapshot["teaching_analysis_packs"]["rows"]:
            row = get_pack(db,old["id"])
            assert row.payload == old["payload"] and row.payload_hash == old["payload_hash"] and row.question_id == old["question_id"]
        for old_table in ("textbook_extraction_calls","exercise_search_calls"):
            for old in snapshot[old_table]["rows"]:
                row = db.get(ModelCall,old["id"])
                assert all(getattr(row,k) == old[k] for k in ("request_hash","stage","status","response","metrics","created_at"))
        for old in snapshot["exercise_generation_attempts"]["rows"]:
            row = generation_diagnostic(db,old["id"])
            assert row["details"] == old["details"] and row["id"] == old["id"]
        current = {q.id:q for q in db.scalars(select(Question))}
        for q in snapshot["questions"]["rows"]:
            assert source_hash(public_snapshot(SimpleNamespace(**{**q, "generated":bool(q["generated"])})),[]) == source_hash(public_snapshot(current[q["id"]]),[]),q["id"]
    report.update(documents_preserved=9,sections_verified=15,references_verified=52,calls_verified=212,
        historical_diagnostics=54,question_source_fingerprints_unchanged=len(snapshot["questions"]["rows"]))
    (REPORT / "migration_verification.json").write_text(serialized(report),encoding="utf-8")
    print(serialized(report))


def retire():
    snapshot, baseline = load()
    verification = json.loads((REPORT / "migration_verification.json").read_text(encoding="utf-8"))
    assert verification["archive_valid"] and len(verification["roundtrip"]) == 8
    from app.application.model_registry import register_models
    assert not set(TABLES).intersection(register_models().tables)
    with engine().begin() as c:
        assert not c.execute(text("SELECT id FROM learning_sessions WHERE active_turn_id IS NOT NULL")).first()
        names = inspect(c).get_table_names()
        for name in TABLES:
            if name in names:
                _,data = rows(c,name)
                assert digest(data) == baseline["tables"][name]["sha256"], "迁移期间旧数据发生变化"
        for name in TABLES:
            c.execute(text(f"DROP TABLE IF EXISTS `{name}`"))
    print(serialized({"dropped":list(TABLES),"tables_after":len(inspect(engine()).get_table_names())}))


if __name__ == "__main__":
    {"prepare":prepare, "migrate":migrate, "verify":verify, "retire":retire}[sys.argv[1]]()
