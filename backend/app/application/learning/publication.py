"""正式发布的普通业务函数；幂等回执和消息共用事务。"""

import json
from langchain_core.messages import HumanMessage, AIMessage
from app.infrastructure.persistence.database import db_session
from app.domains.sessions.models import Message
from app.domains.sessions.service import message_dict
from app.application.learning.receipts import complete, operation_id, owner, receipt, input_row
from app.domains.memory.context import invalidation_epoch


def finish(self, session_id, turn_id, event, result, refs):
    with db_session() as db:
        return complete(db, session_id, turn_id, event, result, refs)


def guidance_messages(self, session_id, event, result):
    """讲题活动的已发布交互增量进入同一父 State，不回填子 Agent 私有推理。"""
    if not result.get("guidance") or not result.get("message"):
        return []
    with db_session() as db:
        user = input_row(db, session_id, event)
        response = result["message"]
        return [HumanMessage(id="formal:" + user.id, content=user.content,
                    additional_kwargs={"tutor_source_refs": ["formal:" + user.id]}),
                AIMessage(id="formal:" + response["id"], content=json.dumps({
                    "problem": result["guidance"]["problem"], "public_reply": response["content"]}, ensure_ascii=False),
                    additional_kwargs={"tutor_source_refs": ["formal:" + response["id"]]})]


async def publish_proposal(self, ctx, event, proposal, assessment=None):
    identifier = operation_id(ctx.session_id, event["request_id"], "response")
    with db_session() as db:
        row = owner(db, ctx.session_id, ctx.turn_id)
        prior = receipt(db, ctx.session_id, event)
        if prior["status"] == "completed":
            return {**prior["outcome"], "replayed": True}
        if invalidation_epoch(db, row.student_id, lock=True) != ctx.memory_epoch:
            raise ValueError("在途教学使用的记忆已撤销，拒绝发布旧候选，请重试。")
        self.planner.save(ctx.session_id, proposal["plan"], event.get("content", ""),
                          turn_id=ctx.turn_id, database=db)
        metadata = dict(proposal["metadata"])
        if proposal["plan"]["action"] == "clarify":
            metadata["clarification"] = row.pending_context.get("decision")
        metadata["learning_progress"] = row.pending_context.get("learning_progress")
        message = db.get(Message, identifier)
        if message is None:
            message = Message(id=identifier, session_id=ctx.session_id, role="assistant", agent="supervisor",
                content=proposal["content"], citations=proposal["citations"],
                metadata_json={**metadata, "request_id": event["request_id"], "_tutoring_order": [row.version, 2]})
            db.add(message)
            db.flush()
            from app.domains.memory.recording.facts import record_message_fact
            record_message_fact(db, message)
        public = message_dict(message)
        result = {**(proposal.get("result") or {}), **(assessment or {}), "plan": proposal["plan"], "message": public}
        if assessment:
            result["next_plan"] = proposal["plan"]
        complete(db, ctx.session_id, ctx.turn_id, event, result, [identifier])
    await ctx.emit("message", public)
    return result


async def publish_feedback(self, ctx, event, result):
    batch = event["kind"] == "batch_answer"
    field = "batch_assessment" if batch else "assessment"
    identifier = operation_id(ctx.session_id, event["request_id"], "feedback")
    with db_session() as db:
        row = owner(db, ctx.session_id, ctx.turn_id)
        message = db.get(Message, identifier)
        if message is None:
            diagnosis = result.get("diagnosis", {})
            labels = {"correct": "回答正确", "partial": "部分正确", "incorrect": "需要再巩固", "ungraded": "需要补充说明"}
            content = result["summary"] if batch else f"## {labels[result['result']]}\n\n{diagnosis.get('feedback', '')}\n\n**诊断依据**：{diagnosis.get('reflection', '')}"
            message = Message(id=identifier, session_id=ctx.session_id, role="assistant", agent="assessment",
                content=content, citations=[], metadata_json={"kind": field, field: result, "_tutoring_order": [row.version, 1]})
            db.add(message)
            db.flush()
            from app.domains.memory.recording.facts import record_message_fact
            record_message_fact(db, message)
        public = message_dict(message)
    await ctx.emit("message", public)
    await ctx.emit("batch_assessment_completed" if batch else "assessment_completed", result)
