"""第三批显式迁移工具；先保存基线，再逐项合并、核验和退表。"""

from pathlib import Path
import sys
import gzip
import hashlib
import json
import zipfile
from datetime import datetime

sys.path.insert(0,str(Path(__file__).resolve().parent))
from simplify_p0 import ROOT, encode, decode, serialized, digest, rows, source_manifest
from sqlalchemy import MetaData, Table, inspect, select, text
from app.infrastructure.persistence.database import engine

TABLES = ("student_memories", "session_experiences", "memory_session_states", "memory_scope_states",
          "memory_dependencies", "memory_scope_jobs", "difficulty_profiles", "difficulty_scores", "question_concept_weights")
ARCHIVE = ROOT / "data/archives/simplification_p2/20261023"
REPORT = ROOT / "data/reports/simplification_p2"


def prepare():
    if (ARCHIVE / "snapshot.json.gz").exists():
        raise RuntimeError("基线已存在，禁止覆盖")
    ARCHIVE.mkdir(parents=True,exist_ok=True)
    REPORT.mkdir(parents=True,exist_ok=True)
    snapshot, baseline = {}, {"source":source_manifest(), "tables":{}}
    with engine().connect() as c:
        assert not c.execute(text("SELECT id FROM learning_sessions WHERE active_turn_id IS NOT NULL")).first(), "仍有在途会话"
        for name in inspect(c).get_table_names():
            _, data = rows(c,name)
            baseline["tables"][name] = {"count":len(data), "sha256":digest(data)}
            snapshot[name] = {"ddl":c.execute(text(f"SHOW CREATE TABLE `{name}`")).one()[1], "rows":data}
    raw = serialized(snapshot).encode()
    (ARCHIVE / "snapshot.json.gz").write_bytes(gzip.compress(raw))
    baseline["archive_sha256"] = hashlib.sha256(raw).hexdigest()
    (REPORT / "before.json").write_text(serialized(baseline),encoding="utf-8")
    with zipfile.ZipFile(ARCHIVE / "source_before.zip","w",zipfile.ZIP_DEFLATED) as z:
        for name in baseline["source"]:
            z.write(ROOT / name,name)
        for p in (ROOT / "backend/app").rglob("*.md"):
            z.write(p,p.relative_to(ROOT).as_posix())
    print(serialized({"tables":len(snapshot),"retiring":{n:len(snapshot[n]["rows"]) for n in TABLES}}))


def load():
    raw = gzip.decompress((ARCHIVE / "snapshot.json.gz").read_bytes())
    baseline = json.loads((REPORT / "before.json").read_text(encoding="utf-8"))
    assert hashlib.sha256(raw).hexdigest() == baseline["archive_sha256"]
    return json.loads(raw,object_hook=decode),baseline


def migrate():
    if (REPORT / "migration.json").exists():
        raise RuntimeError("迁移已执行；请核验，不可重放覆盖当前业务")
    from collections import defaultdict, Counter
    from types import SimpleNamespace, ModuleType
    from app.application.model_registry import register_models
    from app.domains.memory.scopes.identity import scope_key
    from app.domains.courses.difficulty.metadata import fingerprint as question_fingerprint
    from app.agent.context.builder import fingerprint as context_fingerprint
    from app.application.agent.summary import HistorySummary
    from app.agent.context.validation import validate_source_refs
    from app.domains.memory.concepts.service import RULE_VERSION
    snapshot,baseline=load()
    metadata=register_models()
    assert len(metadata.tables)==28
    additions={
        "questions":{"difficulty_estimate":{}},
        "scope_memories":{"generation":0,"invalidation_version":0,"enabled":True,"blocked_source_refs":[],
            "blocked_claim_ids":[],"cutoff_at":None,"legacy_barrier":{}},
        "session_memory_extractions":{"next_seq":0,"last_enqueued_seq":0,"closed":False,"close_version":0,
            "invalidation_version":0,"content":"","concept_tags":[],"experience_source_refs":[],
            "blocked_source_refs":[],"experience_covered_seq":0,"experience_version":0,"experience_status":"provisional"},
        "memory_jobs":{"job_kind":"session_extract","scope_key":None,"source_generation":0,"rule_version":"",
            "result_snapshot":{},"input_fingerprint":"","lease_token":""}}
    with engine().begin() as c:
        for name,values in additions.items():
            known={x["name"] for x in inspect(c).get_columns(name)}
            for key,default in values.items():
                column=metadata.tables[name].c[key]
                typ=column.type.compile(dialect=c.dialect)
                if key not in known:
                    c.execute(text(f"ALTER TABLE `{name}` ADD COLUMN `{key}` {typ} NULL"))
                if default is not None:
                    c.execute(metadata.tables[name].update().where(column.is_(None)).values({key:default}))
                    c.execute(text(f"ALTER TABLE `{name}` MODIFY `{key}` {typ} NOT NULL"))
        c.execute(text("ALTER TABLE memory_jobs MODIFY session_id VARCHAR(36) NULL"))
        indexes={r["name"] for r in inspect(c).get_indexes("memory_jobs")}
        if "uq_memory_job_watermark" in indexes:
            c.execute(text("ALTER TABLE memory_jobs DROP INDEX uq_memory_job_watermark"))
        c.execute(text("ALTER TABLE memory_jobs ADD UNIQUE KEY uq_memory_job_watermark (job_kind,session_id,target_seq,extractor_version,close_version)"))
        assert not c.execute(text("SELECT id FROM learning_sessions WHERE active_turn_id IS NOT NULL")).first()
        if "uq_memory_job_scope" not in indexes:
            c.execute(text("ALTER TABLE memory_jobs ADD UNIQUE KEY uq_memory_job_scope (job_kind,scope_key,source_generation,rule_version)"))
        if "source_units" in {r["name"] for r in inspect(c).get_columns("context_checkpoints")}:
            c.execute(text("ALTER TABLE context_checkpoints MODIFY source_units JSON NULL"))
    sessions={r["id"]:r for r in snapshot["learning_sessions"]["rows"]}
    owners={}
    students={r["student_id"] for r in sessions.values()} | {r["student_id"] for r in snapshot["student_memories"]["rows"]}
    for student in students:
        owners[scope_key(student,"student")]=(student,"student","","")
        for course in snapshot["courses"]["rows"]:
            owners[scope_key(student,"course",course["id"])]=(student,"course",course["id"],"")
        for concept in snapshot["concepts"]["rows"]:
            owners[scope_key(student,"concept",concept["course_id"],concept["id"])]=(student,"concept",concept["course_id"],concept["id"])
    scope_rows={r["scope_key"]:dict(r) for r in snapshot["scope_memories"]["rows"]}
    for state in snapshot["memory_scope_states"]["rows"]:
        key=state["scope_key"]
        student,kind,course,concept=owners[key]
        row=scope_rows.setdefault(key,{"scope_key":key,"student_id":student,"scope_kind":kind,"course_id":course,"concept_id":concept})
        row.update(state)
    for old in snapshot["student_memories"]["rows"]:
        key=scope_key(old["student_id"],"student")
        row=scope_rows.setdefault(key,{"scope_key":key,"student_id":old["student_id"],"scope_kind":"student","course_id":"","concept_id":""})
        row.update(content=old["content"],source_refs=old["source_refs"],version=old["version"],
            status="historical_reference",legacy_barrier={"blocked_source_refs":old["blocked_source_refs"]})
        row["updated_at"]=max(row.get("updated_at",old["updated_at"]),old["updated_at"])
    extraction_rows={r["session_id"]:dict(r) for r in snapshot["session_memory_extractions"]["rows"]}
    for old in snapshot["memory_session_states"]["rows"]:
        session=sessions[old["session_id"]]
        row=extraction_rows.setdefault(session["id"],{"session_id":session["id"],"student_id":session["student_id"],"course_id":session["course_id"]})
        row.update(old)
    for old in snapshot["session_experiences"]["rows"]:
        row=extraction_rows.setdefault(old["session_id"],{"session_id":old["session_id"],"student_id":old["student_id"],"course_id":old["course_id"]})
        row.update(content=old["content"],concept_tags=old["concept_tags"],experience_source_refs=old["source_refs"],
            blocked_source_refs=old["blocked_source_refs"],experience_covered_seq=old["covered_seq"],
            experience_version=old["version"],experience_status=old["status"])
        row["updated_at"]=max(row.get("updated_at",old["updated_at"]),old["updated_at"])
    with engine().begin() as c:
        for table,data,key in (("scope_memories",scope_rows,"scope_key"),("session_memory_extractions",extraction_rows,"session_id")):
            target=metadata.tables[table]
            for identifier,row in data.items():
                if c.execute(select(target.c[key]).where(target.c[key]==identifier)).first():
                    c.execute(target.update().where(target.c[key]==identifier).values(**row))
                else:
                    c.execute(target.insert().values(**row))
        jobs=metadata.tables["memory_jobs"]
        for old in snapshot["memory_scope_jobs"]["rows"]:
            c.execute(jobs.insert().values(**old,job_kind="scope_aggregate",session_id=None))
        c.execute(jobs.update().where(jobs.c.job_kind=="session_extract",jobs.c.extractor_version!=RULE_VERSION,
            jobs.c.status.in_(["pending","retry","running"])).values(status="superseded",lease_until=None))
        for old in snapshot["memory_jobs"]["rows"]:
            c.execute(jobs.update().where(jobs.c.id==old["id"]).values(updated_at=old["updated_at"]))
        checkpoints=metadata.tables["context_checkpoints"]
        latest={}
        for row in snapshot["context_checkpoints"]["rows"]:
            validate_source_refs(HistorySummary.model_validate(row["summary"]),row["source_units"])
            key=(row["session_id"],row["agent"])
            if key not in latest or row["created_at"]>latest[key]["created_at"]:
                latest[key]=row
        c.execute(checkpoints.delete())
        for (session,agent),old in latest.items():
            value={k:v for k,v in old.items() if k!="source_units"}
            value["id"]=context_fingerprint([session,agent])
            value["rule_version"]="learning-context-v3"
            c.execute(checkpoints.insert().values(**value))
        goals=metadata.tables["session_goal_states"]
        for old in snapshot["session_goal_states"]["rows"]:
            nodes=[{**{k:v for k,v in node.items() if k!="depends_on"},"prerequisites":node.get("depends_on",[])} for node in old["nodes"]]
            assert len(nodes)<=32 and len(old["return_stack"])<=4
            c.execute(goals.update().where(goals.c.session_id==old["session_id"]).values(nodes=nodes,updated_at=old["updated_at"]))
    with zipfile.ZipFile(ARCHIVE / "source_before.zip") as z:
        rubric=ModuleType("archived_rubric")
        exec(z.read("backend/app/domains/courses/difficulty/rubric.py").decode("utf-8"),rubric.__dict__)
    names={r["id"]:r["name"] for r in snapshot["concepts"]["rows"]}
    profiles=[p for p in snapshot["difficulty_profiles"]["rows"] if p["status"]=="ready"]
    assert len({p["course_id"] for p in profiles})==len(profiles), "课程有多个可用口径，必须明确选择"
    selected={p["id"]:p for p in profiles}
    scores={r["entity_id"]:r for r in snapshot["difficulty_scores"]["rows"] if r["profile_id"] in selected and r["entity_type"]=="question"}
    statuses=Counter()
    with engine().begin() as c:
        questions=metadata.tables["questions"]
        for old in snapshot["questions"]["rows"]:
            validation=dict(old["validation"] or {})
            validation.pop("difficulty_estimate",None)
            values={"validation":validation}
            score=scores.get(old["id"])
            if score:
                profile=selected[score["profile_id"]]
                config=profile["config"]
                assert rubric.digest(rubric.question_input(SimpleNamespace(**old),names))==score["source_hash"]
                estimate={"value":float(score["value"]) if score["value"] is not None else None,"status":score["status"],"method":profile["method"],"version":profile["version"],
                    "legacy_profile_id":profile["id"],"source_hash":score["source_hash"],"model":profile["model"],
                    "reference_group":profile["reference_group"],"scale_max":config["scale_max"],"higher_is_harder":config["higher_is_harder"],
                    "question_fingerprint":question_fingerprint(SimpleNamespace(**old)),"details":score["details"],
                    "confidence":score["details"].get("confidence"),"updated_at":score["updated_at"].isoformat()}
                values["difficulty_estimate"]=estimate
                statuses[score["status"]]+=1
            c.execute(questions.update().where(questions.c.id==old["id"]).values(**values))
    report={"scope_rows":len(scope_rows),"session_extraction_rows":len(extraction_rows),"checkpoints_before":len(snapshot["context_checkpoints"]["rows"]),
        "checkpoints_after":len(latest),"goals_preserved":len(snapshot["session_goal_states"]["rows"]),
        "difficulty_sources":{p["course_id"]:p["id"] for p in profiles},"difficulty_statuses":dict(statuses),"difficulty_source_mismatches":0}
    (REPORT/"migration.json").write_text(serialized(report),encoding="utf-8")
    print(serialized(report))


def verify():
    from app.application.model_registry import register_models
    from app.domains.memory.scopes.identity import scope_key
    from app.domains.courses.difficulty.metadata import estimate
    from types import SimpleNamespace
    snapshot,baseline = load()
    report = {"archive_valid":True,"roundtrip":{},"unchanged_tables":[],"mapping_checks":{}}
    metadata = register_models()
    with engine().begin() as c:
        for name in TABLES:
            archived = snapshot[name]
            probe = "p2_restore_"+name
            c.execute(text(archived["ddl"].replace(f"CREATE TABLE `{name}`",f"CREATE TEMPORARY TABLE `{probe}`",1)))
            target = Table(probe,MetaData(),autoload_with=c)
            data = archived["rows"]
            for start in range(0,len(data),100):
                c.execute(target.insert(),data[start:start+100])
            restored = [dict(row) for row in c.execute(select(target).order_by(*target.primary_key.columns)).mappings()]
            assert digest(restored)==digest(data),name
            c.execute(text(f"DROP TEMPORARY TABLE `{probe}`"))
            report["roundtrip"][name]=len(data)
        changed = set(TABLES) | {"scope_memories","session_memory_extractions","memory_jobs","context_checkpoints","session_goal_states","questions"}
        for name,old in baseline["tables"].items():
            if name not in changed:
                _,current = rows(c,name)
                assert digest(current)==old["sha256"],name
                report["unchanged_tables"].append(name)
        scopes = {r["scope_key"]:r for r in rows(c,"scope_memories")[1]}
        extractions = {r["session_id"]:r for r in rows(c,"session_memory_extractions")[1]}
        for old in snapshot["memory_scope_states"]["rows"]:
            row = scopes[old["scope_key"]]
            for key in ("generation","invalidation_version","enabled","blocked_source_refs","blocked_claim_ids","cutoff_at"):
                assert row[key]==old[key],(old["scope_key"],key)
        for old in snapshot["student_memories"]["rows"]:
            row = scopes[scope_key(old["student_id"],"student")]
            assert all(row[k]==old[k] for k in ("content","source_refs","version"))
            assert row["legacy_barrier"]["blocked_source_refs"]==old["blocked_source_refs"]
            assert row["status"]=="historical_reference"
        for old in snapshot["memory_session_states"]["rows"]:
            row=extractions[old["session_id"]]
            assert all(row[k]==old[k] for k in ("next_seq","last_enqueued_seq","closed","close_version","invalidation_version"))
        for old in snapshot["session_experiences"]["rows"]:
            row=extractions[old["session_id"]]
            for source,target in (("content","content"),("source_refs","experience_source_refs"),("covered_seq","experience_covered_seq"),
                ("version","experience_version"),("status","experience_status"),("blocked_source_refs","blocked_source_refs"),("concept_tags","concept_tags")):
                assert row[target]==old[source],(old["session_id"],target)
        jobs = {r["id"]:r for r in rows(c,"memory_jobs")[1]}
        for old in snapshot["memory_jobs"]["rows"]:
            assert all(jobs[old["id"]][key]==value for key,value in old.items()),old["id"]
        goals = {r["session_id"]:r for r in rows(c,"session_goal_states")[1]}
        for old in snapshot["session_goal_states"]["rows"]:
            row=goals[old["session_id"]]
            assert all(row[k]==v for k,v in old.items() if k!="nodes")
            assert len(row["nodes"])==len(old["nodes"])
            for before,after in zip(old["nodes"],row["nodes"]):
                assert {k:v for k,v in before.items() if k!="depends_on"}=={k:v for k,v in after.items() if k!="prerequisites"}
                assert after["prerequisites"]==before.get("depends_on",[])
        questions = {r["id"]:r for r in rows(c,"questions")[1]}
        profiles = {r["id"]:r for r in snapshot["difficulty_profiles"]["rows"] if r["status"]=="ready"}
        selected = [r for r in snapshot["difficulty_scores"]["rows"] if r["profile_id"] in profiles and r["entity_type"]=="question"]
        for old in snapshot["questions"]["rows"]:
            row=questions[old["id"]]
            assert all(row[k]==v for k,v in old.items() if k!="validation")
            validation=dict(old["validation"] or {})
            validation.pop("difficulty_estimate",None)
            assert row["validation"]==validation
        for old in selected:
            row=questions[old["entity_id"]]
            value=estimate(SimpleNamespace(**row))
            assert value["legacy_profile_id"]==old["profile_id"]
            assert value["source_hash"]==old["source_hash"]
            assert value["value"] is None if old["value"] is None else abs(value["value"]-float(old["value"]))<1e-9
            assert value["status"]==old["status"]
        caches = rows(c,"context_checkpoints")[1]
        assert len({(r["session_id"],r["agent"]) for r in caches})==len(caches)
        report["mapping_checks"]={"scope_states":len(snapshot["memory_scope_states"]["rows"]),
            "legacy_summaries":len(snapshot["student_memories"]["rows"]),"experiences":len(snapshot["session_experiences"]["rows"]),
            "session_watermarks":len(snapshot["memory_session_states"]["rows"]),"goals":len(goals),"question_scores":len(selected),
            "checkpoints":len(caches),"all_passed":True}
    (REPORT/"migration_verification.json").write_text(serialized(report),encoding="utf-8")
    print(serialized(report))


def retire():
    snapshot,baseline=load()
    verification=json.loads((REPORT/"migration_verification.json").read_text(encoding="utf-8"))
    assert verification["archive_valid"] and len(verification["roundtrip"])==9 and verification["mapping_checks"]["all_passed"]
    from app.application.model_registry import register_models
    assert not set(TABLES).intersection(register_models().tables)
    with engine().begin() as c:
        assert not c.execute(text("SELECT id FROM learning_sessions WHERE active_turn_id IS NOT NULL")).first()
        names=inspect(c).get_table_names()
        for name in TABLES:
            if name in names:
                assert digest(rows(c,name)[1])==baseline["tables"][name]["sha256"],name
        for name in TABLES:
            c.execute(text(f"DROP TABLE IF EXISTS `{name}`"))
        if "source_units" in {r["name"] for r in inspect(c).get_columns("context_checkpoints")}:
            c.execute(text("ALTER TABLE context_checkpoints DROP COLUMN source_units"))
    report={"dropped":list(TABLES),"tables_before":len(baseline["tables"]),"tables_after":len(inspect(engine()).get_table_names())}
    assert report["tables_after"]==28
    (REPORT/"retirement.json").write_text(serialized(report),encoding="utf-8")
    print(serialized(report))


if __name__ == "__main__":
    {"prepare":prepare,"migrate":migrate,"verify":verify,"retire":retire}[sys.argv[1]]()
