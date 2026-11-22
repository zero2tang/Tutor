"""主 Agent 调度四个业务角色，状态变更经过服务校验。"""

from app.application.learning.receipts import operation_id, owner

from app.infrastructure.persistence.database import db_session
from app.domains.sessions.models import LearningSession, Message
from app.agent.runtime.contracts import AgentRunner, EventSink
from app.domains.learning.schemas import MessageRequest
from app.domains.sessions.leases import SessionLease
from app.domains.assessment.service import AssessmentService
from app.domains.courses.catalog import CatalogService
from app.domains.practice.service import PracticeService
from app.domains.practice.context import public_exercise
from app.domains.sessions.service import SessionService
from app.application.learning.planning import LearningPlanner
from app.domains.students.service import StudentService
from app.domains.practice.batches import ExerciseBatchService
from app.domains.assessment.batch import BatchAssessmentService
from app.domains.sessions.progress import preserve_learning_context
from app.application.learning.graph import build_session_graph
from app.application.learning.entry import execute
from app.application.learning.publication import publish_proposal, publish_feedback, finish, guidance_messages
from app.application.learning.actions import apply_action
from app.agent.tools.contracts import ToolContext


class LearningService:
    # 编排层负责调用顺序与持久化；模型只提出决策，不能直接修改会话和掌握度。
    def __init__(self, runner: AgentRunner, *, checkpointer=None):
        """组装四角色共用的执行器，以及规划、习题、评估和学习记录服务。"""
        self.runner = runner
        self.checkpointer = checkpointer
        self.graph = build_session_graph(checkpointer)
        self.planner = LearningPlanner(runner)
        self.sessions, self.catalog, self.student = (
            SessionService(),
            CatalogService(),
            StudentService(),
        )
        from app.application.learning.subgraphs.practice import generation_graph, batch_generation_graph
        self.exercises, self.assessment = PracticeService(runner, generation_graph=generation_graph()), AssessmentService(runner)
        self.batches = ExerciseBatchService(self.exercises, batch_graph=batch_generation_graph())
        self.batch_assessment = BatchAssessmentService(self.assessment)
        from app.application.guidance.service import GuidedTeachingService

        self.leases = SessionLease(self.catalog, database=lambda: db_session())
        self.guidance = GuidedTeachingService(
            runner=runner,
            sessions=self.sessions,
            leases=self.leases,
            catalog=self.catalog,
            student=self.student,
        )

    def claim(self, session_id: str, turn_id: str, *, answering: bool = False) -> dict:
        return self.leases.claim(session_id, turn_id, answering=answering)

    def release(self, session_id: str, turn_id: str, *, failed: bool = False) -> None:
        self.leases.release(session_id, turn_id, failed=failed)

    def owned(self, session_id, turn_id):
        with db_session() as db:
            row = owner(db, session_id, turn_id)
            return {"goal": row.goal, "course_id": row.course_id, "student_id": row.student_id,
                    "concept_ids": list(row.concept_ids), "pending_question_id": row.pending_question_id,
                    "pending_context": dict(row.pending_context)}

    def memory_manifest(self, session_id):
        from app.domains.memory.context import read_context
        with db_session() as db:
            row = db.get(LearningSession, session_id)
            return read_context(db, row.student_id, row.course_id, row.concept_ids, purpose="grading")["memory_manifest"]

    def wait(self, session_id):
        return self.sessions.get(session_id).get("pending_interaction")

    async def execute(self, session_id, event, turn_id, emit):
        return await execute(self, session_id, event, turn_id, emit)

    async def turn(self, session_id, request, turn_id, emit):
        return await self.execute(session_id, {"kind": "message", **request.model_dump(exclude_unset=True)}, turn_id, emit)

    async def answer(self, session_id, request, turn_id, emit):
        return await self.execute(session_id, {"kind": "answer", **request.model_dump()}, turn_id, emit)

    async def answer_batch(self, session_id, request, turn_id, emit):
        return await self.execute(session_id, {"kind": "batch_answer", **request.model_dump()}, turn_id, emit)

    publish_proposal = publish_proposal
    publish_feedback = publish_feedback
    finish = finish
    guidance_messages = guidance_messages
    apply_action = apply_action

    async def orchestrate(
        self,
        session_id,
        state,
        snapshot,
        request,
        turn_id,
        emit,
        *,
        extra_payload=None,
        metadata_extra=None,
        publish=True,
        config=None,
        messages=None,
    ):
        """已取得会话占用后运行主模型；子工具永远返回到这个调用方的上下文。"""
        from app.application.learning.delegation import LearningDelegation

        delegation = LearningDelegation(
            self, state, snapshot, request, turn_id, emit, db_session
        )
        delegation.require_continuation = bool(
            extra_payload and extra_payload.get("assessment_result")
        )
        planned, _ = await self.planner.plan(
            session_id,
            state,
            snapshot,
            request,
            turn_id,
            emit,
            tool_source=delegation,
            config=config,
            messages=messages,
            output_validator=delegation.validate_final,
            effects_applied=lambda: bool(delegation.outcomes),
            extra_payload={
                **(extra_payload or {}),
                "completed_delegations": delegation.outcomes,
            },
        )
        plan = planned["output"]
        await emit("plan", plan)
        content, citations, metadata, result = delegation.presentation(plan)
        citations = list(
            {c["id"]: c for c in [*citations, *planned["citations"]]}.values()
        )[:10]
        metadata.update(metadata_extra or {})
        metadata["plan"] = plan
        if plan["action"] == "clarify":
            metadata["clarification"] = self.sessions.get(session_id)[
                "pending_decision"
            ]
        metadata["learning_progress"] = self.sessions.get(session_id).get(
            "learning_progress"
        )
        if not publish:
            return {"content":content,"citations":citations,"metadata":metadata,"plan":plan,"result":result,"messages":planned.get("messages", messages or [])}
        raise RuntimeError("主回复必须由会话图 publish 节点提交")

    async def pose_contract(
        self,
        session_id: str,
        contract: dict,
        context: ToolContext,
        turn_id: str,
        cognitive_type: str,
        emit: EventSink,
        *, operation_id=None,
    ) -> dict:
        """把选定题目变成可恢复的待答状态。

        冻结含标准答案的内部题目契约、目标知识点和帮助状态；浏览器收到的 question 则经过公开字段过滤。
        """
        if contract.get("course_id") != context.course_id:
            raise ValueError("题目契约不属于当前课程")
        question = public_exercise(contract, context.course_id)
        question["practice_type"] = cognitive_type
        from app.application.learning.receipts import operation_id as stable_id
        from app.domains.sessions.interactions import digest
        if not operation_id and not context.request_id:
            raise ValueError("发布题目必须具有稳定请求身份")
        effect = operation_id or stable_id(session_id, context.request_id, "pose")
        exercise_id = stable_id(session_id, effect, "exercise")
        input_hash = digest([contract, cognitive_type, context.concept_ids])
        with db_session() as db:
            row = owner(db, session_id, turn_id)
            if (
                row is None
                or row.course_id != context.course_id
                or row.student_id != context.student_id
                or row.active_turn_id != turn_id
            ):
                raise ValueError("题目上下文与会话课程不一致")
            prior = db.get(Message, exercise_id)
            if prior:
                if prior.metadata_json.get("_input_hash") != input_hash:
                    raise ValueError("同一发题操作不能对应不同题目")
                return {"question": prior.metadata_json["question"], "exercise_id": prior.id, "replayed": True}
            from app.domains.memory.context import invalidation_epoch
            if context.memory_epoch is not None and invalidation_epoch(db, row.student_id, lock=True) != context.memory_epoch:
                raise ValueError("出题使用的记忆已撤销，拒绝发布旧候选。")
            if row.pending_question_id:
                raise ValueError("已有未完成题目")
            row.pending_question_id = contract["id"]
            row.pending_context = {
                **preserve_learning_context(row.pending_context),
                "contract": contract,
                "exercise_id": exercise_id,
                "assisted": False,
                "cognitive_type": cognitive_type,
                "target_concept_ids": context.concept_ids,
            }
            # 即使主Agent随后失败，本次出题的冻结版本也已有独立、可追问的记录。
            db.add(
                Message(
                    id=exercise_id,
                    session_id=session_id,
                    role="assistant",
                    agent="exercise",
                    content="本次学习中的一道练习。",
                    metadata_json={
                        "kind": "question",
                        "record_only": True,
                        "_operation_id": effect,
                        "_root_request_id": context.request_id,
                        "_input_hash": input_hash,
                        "_tutoring_order": [row.version + 1, 1],
                        "question_id": contract["id"],
                        "question": question,
                        "_exercise_contract": contract,
                    },
                )
            )
            row.phase = "awaiting_answer"
            row.version += 1
        # 展示和后续上下文均使用冻结题干，避免推理期间题库更新造成版本错配。
        await emit("question", question)
        return {"question": question, "exercise_id": exercise_id}

