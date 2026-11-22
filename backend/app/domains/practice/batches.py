"""在一次会话任务内并行生成多题，全部通过校验后统一发布。"""

from app.infrastructure.persistence.database import db_session
from app.domains.sessions.models import LearningSession, Message
from app.domains.practice.schemas import ExerciseBatchRequest
from app.domains.practice.batch_state import public_batch
from app.domains.courses.catalog import CatalogService
from app.domains.practice.service import PracticeService
from app.domains.practice.requirements import generation_assignment
from app.domains.sessions.progress import preserve_learning_context
from app.domains.practice.operations import operation_key, require_owner
from app.domains.sessions.interactions import digest as fingerprint
from app.shared.identity import operation_id as stable_id
from app.agent.tools.contracts import ToolContext


def validate_batch_request(spec: dict, context: ToolContext) -> ExerciseBatchRequest:
    request = ExerciseBatchRequest.model_validate(spec)
    allowed = set(context.concept_ids)
    catalog = CatalogService()
    for task in request.tasks:
        if len(set(task.concept_ids)) != len(task.concept_ids):
            raise ValueError("练习子任务的知识点不能重复")
        if not set(task.concept_ids).issubset(allowed):
            raise ValueError("练习子任务超出当前教学知识点范围")
        for cid in task.concept_ids:
            catalog.concept(cid, context.course_id)
    return request


class ExerciseBatchService:
    def __init__(self, exercises: PracticeService, *, batch_graph=None):
        self.exercises = exercises
        self.batch_graph = batch_graph

    async def generate(
        self, spec, context, turn_id, emit, *, teaching_request="",
        operation_id=None, config=None, runtime_context=None,
    ):
        request = validate_batch_request(spec, context)
        key = operation_key(context, operation_id, "practice_batch")
        batch_id = stable_id(context.session_id, key, "batch")
        first_message_id = stable_id(context.session_id, key, "exercise:1")
        input_hash = fingerprint({
            "session_id": context.session_id,
            "student_id": context.student_id,
            "course_id": context.course_id,
            "request": request.model_dump(mode="json"),
            "teaching_request": teaching_request,
        })

        def existing(db):
            # 完成回执先于待答检查；即使本组已答完，也只能回放原产物。
            message = db.get(Message, first_message_id)
            if message is None:
                return None
            receipt = message.metadata_json.get("batch_operation", {})
            if (
                message.session_id != context.session_id
                or receipt.get("operation_id") != key
                or receipt.get("input_hash") != input_hash
            ):
                raise ValueError("同一练习组操作标识不能对应不同输入")
            return receipt

        with db_session() as db:
            row = require_owner(db, context, turn_id)
            session_version = row.version
            prior = existing(db)
            if not prior and (row.pending_question_id or row.pending_context.get("exercise_batch")):
                raise ValueError("请先提交或跳过当前练习")
        if prior:
            await emit("exercise_batch", prior["public"])
            return {"exercise_batch": prior["public"]}, prior["citations"]
        if self.batch_graph is None:
            raise RuntimeError("应用未装配统一练习组生成图")

        await emit("batch_generation_started", {
            "batch_id": batch_id, "count": len(request.tasks), "title": request.title,
        })
        tasks = [
            {
                "index": index,
                "task_id": f"item:{index}",
                "operation_id": key,
                "concept_ids": list(task.concept_ids),
                "cognitive_type": task.cognitive_type,
                "assignment": generation_assignment(
                    task, teaching_request=teaching_request, index=index,
                    total=len(request.tasks),
                ),
            }
            for index, task in enumerate(request.tasks, 1)
        ]
        # Send 调度并发，失败会取消并等待同批任务；不存在另一条手工并发路径。
        generated_state = await self.batch_graph.ainvoke(
            {"tasks": tasks, "results": {}},
            config={**(config or {}), "max_concurrency": 3},
            context={
                "exercises": self.exercises, "context": context, "turn_id": turn_id,
                "emit": emit, "batch_id": batch_id,
                "runtime_context": runtime_context,
            },
            **({"durability": "sync"} if config and config.get("metadata", {}).get("tutor_durable") else {}),
        )
        generated = [generated_state["results"][task["task_id"]] for task in tasks]
        items = [
            {
                "contract": value["contract"],
                "cognitive_type": task.cognitive_type,
                "target_concept_ids": list(task.concept_ids),
                "assisted": False,
                "exercise_id": stable_id(context.session_id, key, f"exercise:{index}"),
                "generation_run_id": value["result"]["run_id"],
            }
            for index, (task, value) in enumerate(zip(request.tasks, generated), 1)
        ]
        stems = ["".join(item["contract"]["stem"].split()) for item in items]
        identifiers = [item["contract"]["id"] for item in items]
        if len(set(stems)) != len(items) or len(set(identifiers)) != len(items):
            raise ValueError("生成组中存在重复题目，本组未发布，请重试")
        batch = {"id": batch_id, "title": request.title, "items": items}
        public = public_batch(batch, context.course_id)
        citations = list({
            citation["id"]: citation
            for value in generated for citation in value["result"].get("citations", [])
        }.values())[:10]
        with db_session() as db:
            row = require_owner(db, context, turn_id)
            prior = existing(db)
            if prior:
                public, citations = prior["public"], prior["citations"]
            else:
                if row.version != session_version:
                    from app.shared.execution import LearningTurnLost

                    raise LearningTurnLost("练习组发布前会话版本已变化")
                if row.pending_question_id or row.pending_context.get("exercise_batch"):
                    raise ValueError("请先提交或跳过当前练习")
                row.pending_question_id = identifiers[0]
                row.pending_context = {
                    **preserve_learning_context(row.pending_context),
                    "exercise_batch": batch,
                }
                row.phase = "awaiting_answer"
                row.version += 1
                for index, (item, question) in enumerate(zip(items, public["questions"]), 1):
                    db.add(Message(
                        id=item["exercise_id"], session_id=context.session_id,
                        role="assistant", agent="exercise", content="练习组中的一道题。",
                        metadata_json={
                            "kind": "question", "batch_id": batch_id,
                            "_root_request_id": context.request_id,
                            "_tutoring_order": [row.version, 1, index],
                            "question_id": question["id"], "question": question,
                            "_exercise_contract": item["contract"],
                            **({
                                "batch_operation": {
                                    "operation_id": key, "input_hash": input_hash,
                                    "public": public, "citations": citations,
                                }
                            } if item["exercise_id"] == first_message_id else {}),
                        },
                    ))
        await emit("exercise_batch", public)
        return {"exercise_batch": public}, citations
