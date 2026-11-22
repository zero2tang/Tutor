"""显式选题和跳过的事务操作，不再从 HTTP 绕过会话图。"""

from app.infrastructure.persistence.database import db_session
from app.domains.sessions.models import Message
from app.domains.sessions.progress import preserve_learning_context
from app.agent.tools.contracts import ToolContext
from app.application.learning.receipts import complete, operation_id, owner


async def apply_action(self, ctx, event, *, config=None):
    if event["kind"] == "skip":
        with db_session() as db:
            row = owner(db, ctx.session_id, ctx.turn_id)
            workspace = (row.pending_context or {}).get("guided_teaching")
            if workspace and not workspace.get("closed"):
                raise ValueError("当前正在引导讲题，请结束当前讲题工作区。")
            row.pending_question_id = None
            context = row.pending_context or {}
            row.pending_context = {**preserve_learning_context(context),
                **{key: context[key] for key in ("archived_problem", "guided_teaching") if key in context}}
            row.version += 1
            result = {"skipped": True}
            complete(db, ctx.session_id, ctx.turn_id, event, result, [ctx.session_id])
        return result
    snapshot = self.sessions.get(ctx.session_id)
    effect = operation_id(ctx.session_id, event["request_id"], "pose")
    identifier = operation_id(ctx.session_id, effect, "exercise")
    with db_session() as db:
        guided = db.get(Message, operation_id(ctx.session_id, event["request_id"], "guidance_response"))
        if guided and guided.metadata_json.get("_guidance_receipt"):
            from app.domains.sessions.service import message_dict
            saved = guided.metadata_json["_guidance_receipt"]
            result = {"guidance": saved["guidance"], "message": message_dict(guided), "run_id": saved.get("run_id"), "replayed": True}
            complete(db, ctx.session_id, ctx.turn_id, event, result, [guided.id])
            return result
        prior = db.get(Message, identifier)
        if prior:
            result = {"question": prior.metadata_json["question"], "exercise_id": identifier, "replayed": True}
            complete(db, ctx.session_id, ctx.turn_id, event, result, [identifier])
            return result
    if self.guidance.active(snapshot):
        raise ValueError("当前正在引导讲解，请先结束本题或另建会话。")
    question = self.catalog.question(event["question_id"], snapshot["course_id"])
    if snapshot["course_id"] == "junior_algebra" or question.get("teaching_pack_id"):
        from app.domains.guidance.schemas import GuidanceRequest, ExternalProblem
        result = await self.guidance.act_owned(ctx.session_id, GuidanceRequest(action="start",
            problem=ExternalProblem(stem=question["stem"]), request_id=event["request_id"]), ctx.turn_id, ctx.emit, config=config)
        return self.finish(ctx.session_id, ctx.turn_id, event, result, [result["message"]["id"]])
    scope = ToolContext(course_id=snapshot["course_id"], session_id=ctx.session_id,
        student_id=self.owned(ctx.session_id, ctx.turn_id)["student_id"], concept_ids=question["concept_ids"], request_id=event["request_id"], memory_epoch=ctx.memory_epoch)
    contract, _ = await self.exercises.select(scope, ctx.turn_id, event.get("cognitive_type", "understanding"), ctx.emit,
                                             explicit_question_id=event["question_id"], config=config)
    result = await self.pose_contract(ctx.session_id, contract, scope, ctx.turn_id, event.get("cognitive_type", "understanding"), ctx.emit,
                                      operation_id=effect)
    return self.finish(ctx.session_id, ctx.turn_id, event, result, [result["exercise_id"]])
