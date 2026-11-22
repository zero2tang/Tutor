"""第四批基线：完整实验库与本批源码归档；S14 不在范围内。"""
from pathlib import Path
import sys,gzip,hashlib,json,zipfile
sys.path.insert(0,str(Path(__file__).resolve().parent))
from simplify_p0 import ROOT,serialized,digest,rows,source_manifest,decode
from sqlalchemy import inspect,text
from app.infrastructure.persistence.database import engine
ARCHIVE=ROOT/"data/archives/simplification_p3/20261023"
REPORT=ROOT/"data/reports/simplification_p3"


def prepare():
    assert not (ARCHIVE/"snapshot.json.gz").exists(),"基线已存在，不能覆盖"
    ARCHIVE.mkdir(parents=True,exist_ok=True);REPORT.mkdir(parents=True,exist_ok=True)
    baseline={"source":source_manifest(),"tables":{}};snapshot={}
    with engine().connect() as c:
        assert not c.execute(text("SELECT id FROM learning_sessions WHERE active_turn_id IS NOT NULL")).first(),"仍有在途会话"
        for name in inspect(c).get_table_names():
            data=rows(c,name)[1]
            snapshot[name]={"ddl":c.execute(text(f"SHOW CREATE TABLE `{name}`")).one()[1],"rows":data}
            baseline["tables"][name]={"count":len(data),"sha256":digest(data)}
    assert len(snapshot)==28
    raw=serialized(snapshot).encode();baseline["archive_sha256"]=hashlib.sha256(raw).hexdigest()
    (ARCHIVE/"snapshot.json.gz").write_bytes(gzip.compress(raw))
    (REPORT/"before.json").write_text(serialized(baseline),encoding="utf-8",newline="\n")
    with zipfile.ZipFile(ARCHIVE/"source_before.zip","w",zipfile.ZIP_DEFLATED) as z:
        for name in baseline["source"]:z.write(ROOT/name,name)
        for folder in ("backend/app","backend/tests","frontend/src","scripts","docs"):
            for p in (ROOT/folder).rglob("*"):
                if p.is_file() and p.suffix in {".py",".md",".ts",".tsx",".mjs",".json"} and "__pycache__" not in p.parts and p.relative_to(ROOT).as_posix() not in baseline["source"]:
                    z.write(p,p.relative_to(ROOT).as_posix())
        for p in [ROOT/"README.md",*(ROOT/"backend").glob("requirements*.txt")]:z.write(p,p.relative_to(ROOT).as_posix())
    print(serialized({"tables":28,"production_files":len(baseline["source"])}))


def load():
    raw=gzip.decompress((ARCHIVE/"snapshot.json.gz").read_bytes())
    baseline=json.loads((REPORT/"before.json").read_text(encoding="utf-8"))
    assert hashlib.sha256(raw).hexdigest()==baseline["archive_sha256"]
    return json.loads(raw,object_hook=decode),baseline


def migrate():
    from copy import deepcopy
    from collections import defaultdict
    from app.application.model_registry import register_models
    from app.domains.guidance.history import thin_workspace,support_receipt,observation_receipt,span
    snapshot,baseline=load()
    assert not (REPORT/"migration.json").exists(),"已迁移，不能覆盖现有业务"
    metadata=register_models()
    messages={r["id"]:deepcopy(r) for r in snapshot["messages"]["rows"]}
    assistants={};users={}
    for row in messages.values():
        m=row["metadata_json"]
        if row["role"]=="assistant" and m.get("workspace_id") and m.get("request_id"):
            assistants[(row["session_id"],m["workspace_id"],m["request_id"])]=row
        if row["role"]=="user" and m.get("guidance_request_id"):
            users[(row["session_id"],m["guidance_request_id"])]=row
    changed_messages=set();updates={};observations=supports=before_bytes=after_bytes=0
    retired={"selected_material_ids","material_transfer_receipt","transferable_materials","delegated_materials","tool_failures"}
    def clean(value):
        if isinstance(value,dict):return {k:clean(v) for k,v in value.items() if k not in retired}
        if isinstance(value,list):return [clean(v) for v in value]
        return value
    for session in snapshot["learning_sessions"]["rows"]:
        context=deepcopy(session["pending_context"])
        checkpoint=context.get("delegation_checkpoint")
        if checkpoint:
            checkpoint=clean(checkpoint)
            for outcome in checkpoint.get("outcomes",[]):
                value={k:outcome[k] for k in ("role","name","arguments")}
                outcome["key"]=hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            context["delegation_checkpoint"]=checkpoint
        state=context.get("guided_teaching")
        if state:
            before_bytes+=len(serialized(state).encode())
            refs=[]
            by_request={}
            for evidence in state.get("evidence",[]):
                key=(session["id"],state["id"],evidence["request_id"])
                message=assistants.get(key);user=users.get((session["id"],evidence["request_id"]))
                assert message and user,"讲题原答或输出引用缺失，不能删除副本"
                message["metadata_json"]["_guidance_observation"]=observation_receipt(evidence,user["id"])
                if evidence["raw_input"]!=user["content"]:
                    user["metadata_json"]["_guidance_input_text"]=evidence["raw_input"]
                    changed_messages.add(user["id"])
                message["metadata_json"]["_guidance_support"]=[]
                public=message["metadata_json"].get("_guidance_receipt",{}).get("guidance",{})
                dialogue=next((d for d in state.get("dialogue",[]) if d.get("question_id") and d.get("question_id")==public.get("pending_question_id")),None)
                message["metadata_json"]["_guidance_body_span"]=span(message["content"],dialogue["content"]) if dialogue else [0,len(message["content"])]
                refs.append(message["id"]);by_request[evidence["request_id"]]=message
                changed_messages.add(message["id"]);observations+=1
            for event in state.get("support_history",[]):
                message=by_request.get(event.get("request_id"))
                if message is None and event.get("detection_method")=="server_legacy_policy":message=messages[refs[0]]
                assert message,"帮助记录无法关联已发布消息"
                message["metadata_json"]["_guidance_support"].append(support_receipt(event,message["content"]))
                supports+=1
            assert not state.get("prior_observations"),"历史前题观察须先定位消息，不能静默丢弃"
            if state.get("analysis") is not None:
                messages[refs[-1]]["metadata_json"]["_guidance_analysis"]=deepcopy(state["analysis"])
                messages[refs[-1]]["metadata_json"]["_guidance_analysis_provenance"]="legacy_workspace_latest"
            state["history_refs"]=refs;state["session_id"]=session["id"]
            context["guided_teaching"]=thin_workspace(state)
            after_bytes+=len(serialized(context["guided_teaching"]).encode())
        if context!=session["pending_context"]:updates[session["id"]]=context
    previous=0
    for row in messages.values():
        if "_previous_guidance_workspace" in row["metadata_json"]:
            raise RuntimeError("存在未定位的完整旧工作区快照，先迁移引用再删除")
    with engine().begin() as c:
        assert not c.execute(text("SELECT id FROM learning_sessions WHERE active_turn_id IS NOT NULL")).first()
        for name in ("learning_sessions","messages"):
            assert digest(rows(c,name)[1])==baseline["tables"][name]["sha256"],"基线后已有业务写入"
        target=metadata.tables["learning_sessions"]
        original={r["id"]:r for r in snapshot["learning_sessions"]["rows"]}
        for key,value in updates.items():
            c.execute(target.update().where(target.c.id==key).values(pending_context=value,updated_at=original[key]["updated_at"]))
        target=metadata.tables["messages"]
        for key in changed_messages:
            c.execute(target.update().where(target.c.id==key).values(metadata_json=messages[key]["metadata_json"]))
    report={"sessions_changed":len(updates),"message_metadata_changed":len(changed_messages),"observations_moved":observations,
        "support_receipts_moved":supports,"workspace_bytes_before":before_bytes,"workspace_bytes_after":after_bytes,
        "tables_before":28,"tables_after":len(inspect(engine()).get_table_names()),"dropped_tables":[],"old_previous_snapshots":previous}
    (REPORT/"migration.json").write_text(serialized(report),encoding="utf-8",newline="\n")
    print(serialized(report))


def verify():
    from app.application.model_registry import register_models
    from app.infrastructure.persistence.database import db_session
    from app.domains.sessions.models import LearningSession,Message
    from app.domains.guidance.history import hydrate,TRANSIENT
    snapshot,baseline=load();register_models()
    report={"unchanged_tables":[],"observations_verified":0,"supports_verified":0,"archive_valid":True}
    with engine().connect() as c:
        assert len(inspect(c).get_table_names())==28
        for name,old in baseline["tables"].items():
            if name not in {"learning_sessions","messages"}:
                assert digest(rows(c,name)[1])==old["sha256"],name
                report["unchanged_tables"].append(name)
    with db_session() as db:
        for old in snapshot["messages"]["rows"]:
            row=db.get(Message,old["id"])
            assert all(getattr(row,k)==v for k,v in old.items() if k!="metadata_json"),old["id"]
        for old in snapshot["learning_sessions"]["rows"]:
            row=db.get(LearningSession,old["id"])
            assert all(getattr(row,k)==v for k,v in old.items() if k!="pending_context"),old["id"]
            context=row.pending_context
            for key,value in old["pending_context"].items():
                if key not in {"guided_teaching","delegation_checkpoint"}:assert context[key]==value
            if state:=old["pending_context"].get("guided_teaching"):
                stored=context["guided_teaching"]
                assert not set(TRANSIENT).intersection(stored)
                reconstructed=hydrate(db,stored)
                assert reconstructed["analysis"]==state.get("analysis")
                assert len(reconstructed["evidence"])==len(state["evidence"])
                for before,after in zip(state["evidence"],reconstructed["evidence"]):
                    assert all(after.get(k)==v for k,v in before.items()),before["id"]
                    report["observations_verified"]+=1
                assert reconstructed["support_history"]==state.get("support_history",[])
                report["supports_verified"]+=len(state.get("support_history",[]))
    report["all_passed"]=True
    (REPORT/"migration_verification.json").write_text(serialized(report),encoding="utf-8",newline="\n")
    print(serialized(report))


def compact_analysis():
    """将当前私有参考解变为消息私有附件，换题后仍可按消息审计。"""
    from copy import deepcopy
    from app.application.model_registry import register_models
    from app.infrastructure.persistence.database import db_session
    from app.domains.sessions.models import LearningSession,Message
    from app.domains.guidance.history import thin_workspace
    count=0;total_bytes=0
    with db_session() as db:
        from sqlalchemy import select
        for row in db.scalars(select(LearningSession)):
            context=deepcopy(row.pending_context)
            if not (state:=context.get("guided_teaching")):continue
            if state.get("analysis") is not None:
                message=db.get(Message,state["history_refs"][-1])
                assert message.session_id==row.id and message.metadata_json["workspace_id"]==state["id"]
                message.metadata_json={**message.metadata_json,"_guidance_analysis":state["analysis"],
                    "_guidance_analysis_provenance":"legacy_workspace_latest"}
                stamp=row.updated_at
                context["guided_teaching"]=thin_workspace(state)
                row.pending_context=context
                # 本次仅调整存储位置，历史更新时间不变。
                db.flush()
                from sqlalchemy import update
                db.execute(update(LearningSession).where(LearningSession.id==row.id).values(updated_at=stamp))
                count+=1
            total_bytes+=len(serialized(context["guided_teaching"]).encode())
    report=json.loads((REPORT/"migration.json").read_text(encoding="utf-8"))
    report.update(private_analysis_attachments=count,workspace_bytes_after=total_bytes)
    (REPORT/"migration.json").write_text(serialized(report),encoding="utf-8",newline="\n")
    print(serialized({"private_analysis_attachments":count,"workspace_bytes_after":total_bytes}))

if __name__=="__main__":
    {"prepare":prepare,"migrate":migrate,"compact_analysis":compact_analysis,"verify":verify}[sys.argv[1]]()
