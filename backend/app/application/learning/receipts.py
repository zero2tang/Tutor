"""正式输入和完成回执复用消息主键，与图检查点分开提交。"""

from copy import deepcopy
from app.shared.identity import operation_id
from sqlalchemy import func, select

from app.domains.sessions.interactions import digest
from app.domains.sessions.models import LearningSession, Message
from app.shared.execution import LearningTurnLost, LearningConflict


def owner(db, session_id, turn_id):
    row = db.get(LearningSession, session_id, with_for_update=True)
    if not turn_id or row is None or row.active_turn_id != turn_id:
        raise LearningTurnLost("本次运行已失去会话执行权")
    return row


def input_row(db, session_id, event):
    direct = db.get(Message, operation_id(session_id, event["request_id"], "input"))
    return direct or db.scalar(select(Message).where(Message.session_id == session_id,
        Message.role == "user", Message.metadata_json["_native_request"]["request_id"].as_string() == event["request_id"]))


def receipt(db, session_id, event):
    row = input_row(db, session_id, event)
    stored = (row.metadata_json or {}).get("_native_request") if row else None
    if stored:
        if stored.get("payload_hash") is None:
            verified = stored.get("verified_input")
            if not verified or any(event.get(key) != value for key, value in verified.items()):
                raise LearningConflict("历史请求缺少可核验的同一输入，请读取已保存结果或使用新请求标识。")
            unknown = set(event) - set(verified) - {"request_id", "wait_id"}
            if unknown:
                raise LearningConflict("历史请求未保存这些输入偏好，请使用新请求标识。")
        elif stored["payload_hash"] != digest(event):
            raise LearningConflict("同一请求标识不能对应不同输入")
    return deepcopy(stored)


def formal_effects(db, session_id, request_id):
    """只识别绑定本请求的正式产物，不把运行日志当作业务提交凭证。"""
    refs = []
    for message in db.scalars(select(Message).where(Message.session_id == session_id, Message.role == "assistant")):
        meta = message.metadata_json or {}
        if (meta.get("_root_request_id") == request_id or meta.get("request_id") == request_id
            or meta.get("_delegation_receipt", {}).get("request_id") == request_id
            or meta.get("_guidance_receipt", {}).get("request_id") == request_id):
            refs.append(message.id)
    from app.domains.assessment.models import Attempt, BatchSubmission
    refs.extend(row.id for row in db.scalars(select(Attempt).where(Attempt.session_id == session_id, Attempt.idempotency_key == request_id)))
    refs.extend(row.id for row in db.scalars(select(BatchSubmission).where(BatchSubmission.session_id == session_id, BatchSubmission.idempotency_key == request_id)))
    return refs


def abandon(db, session_id, turn_id, event):
    """新输入终止已释放执行权的旧计算；原答和已提交效果继续保留。"""
    owner(db, session_id, turn_id)
    row = input_row(db, session_id, event)
    if row:
        stored = receipt(db, session_id, event)
        if stored["status"] == "received":
            row.metadata_json = {**row.metadata_json, "_native_request": {**stored, "status": "abandoned",
                "result_refs": formal_effects(db, session_id, event["request_id"])}}


def receive(db, session_id, turn_id, event):
    session = owner(db, session_id, turn_id)
    prior = receipt(db, session_id, event)
    if prior:
        return prior
    text = event.get("answer") or event.get("content") or event.get("text") or (event.get("problem") or {}).get("stem") or {"skip": "跳过当前练习。", "batch_answer": "提交整组练习答案。", "practice_existing": "选择题库练习。"}.get(event["kind"], "继续当前学习操作。")
    stored = {"schema_version": 2, "request_id": event["request_id"], "payload_hash": digest(event),
              "status": "received", "event": event, "result_refs": []}
    public_input = ({"batch_id": event["batch_id"], "answers": {item["question_id"]: item["answer"] for item in event["answers"]}}
                    if event["kind"] == "batch_answer" else {})
    # 正式输入与本轮助手按业务版本配对；失败输入共用版本时，用累计序号保持原接收顺序。
    ordinal = db.scalar(select(func.count()).select_from(Message).where(Message.session_id == session_id, Message.role == "user"))
    row = Message(id=operation_id(session_id, event["request_id"], "input"), session_id=session_id,
                  role="user", agent="", content=text, citations=[],
                  metadata_json={"request_id": event["request_id"], "kind": event["kind"], **public_input,
                    "_tutoring_order": [session.version + 1, 0, ordinal], "_native_request": stored})
    db.add(row)
    db.flush()
    from app.domains.memory.recording.facts import record_message_fact
    record_message_fact(db, row)
    return stored


def complete(db, session_id, turn_id, event, outcome, refs):
    owner(db, session_id, turn_id)
    row = input_row(db, session_id, event)
    if row is None:
        raise ValueError("正式输入尚未接收")
    stored = receipt(db, session_id, event)
    if stored["status"] == "completed":
        return stored["outcome"]
    if not refs:
        raise ValueError("完成回执必须引用正式产物")
    value = {k: v for k, v in outcome.items() if k not in {"session", "messages"}}
    row.metadata_json = {**row.metadata_json, "_native_request": {**stored, "status": "completed", "result_refs": list(refs), "outcome": value}}
    return value
