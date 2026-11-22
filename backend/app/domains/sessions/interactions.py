"""从正式待办推导业务等待标识，不依赖临时图 interrupt ID。"""

from hashlib import sha256
import json


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def pending_interaction(session, messages=()):
    context = session.pending_context or {}
    batch = context.get("exercise_batch")
    if session.pending_question_id:
        if batch:
            source, kind = batch["id"], "batch_answer"
            version = digest([item["contract"] for item in batch["items"]])
            extra = {"batch_id": source}
        else:
            contract = context.get("contract", {})
            source = next((m.id for m in reversed(list(messages)) if (m.metadata_json or {}).get("question_id") == session.pending_question_id and m.role == "assistant"), session.pending_question_id)
            kind, version = "answer", digest(contract)
            extra = {"question_id": session.pending_question_id}
    else:
        workspace = context.get("guided_teaching", {})
        if workspace and not workspace.get("closed") :
            source, kind, version = workspace["id"], "guidance", str(workspace.get("version", 0))
            extra = {"workspace_id": source, "version": workspace.get("version", 0), "requires_response": workspace.get("response_expectation", "answer") == "answer"}
        elif context.get("decision"):
            source, kind, version = session.id, "clarify", digest(context["decision"])
            extra = {}
        else:
            return None
    return {"wait_id": digest([session.id, kind, source, version]), "kind": kind,
            "source_id": source, "version": version, **extra}
