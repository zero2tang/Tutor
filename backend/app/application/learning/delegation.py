"""一次学习轮次内的业务子 Agent 工具；结果先回调用方，不直接发布最终答复。"""

from dataclasses import replace
import hashlib
import json

from app.application.learning.presentation import present_delegations
from app.domains.sessions.models import LearningSession, Message
from sqlalchemy import select
from app.application.learning.receipts import operation_id, owner
from app.agent.runtime.tool_policy import MAX_DELEGATIONS
from app.domains.learning.schemas import Lesson
from app.domains.memory.context import invalidation_epoch
from app.domains.practice.batch_state import mark_batch_assisted
from app.domains.practice.batches import validate_batch_request
from app.domains.practice.context import SessionExerciseService
from app.domains.practice.requirements import (
    generation_assignment,
    validate_generation_operation,
)
from app.shared.execution import LearningTurnLost, LearningConflict


class LearningDelegation:
    """把受约束的教学能力绑定到当前会话，不修改共享注册表。"""

    def __init__(self, learning, state, snapshot, request, turn_id, emit, db_session):
        self.learning, self.state, self.snapshot = learning, state, snapshot
        self.request, self.turn_id, self.emit = request, turn_id, emit
        self.db_session = db_session
        self.calls = 0
        self.consumed = {}
        self.require_continuation = False
        self.outcomes = []
        # 已接受业务结果以正式消息回执保存，原生图独自维护执行游标。
        with self.db_session() as db:
            for row in db.scalars(select(Message).where(Message.session_id == snapshot["id"])):
                value = (row.metadata_json or {}).get("_delegation_receipt")
                if value and value["request_id"] == request.request_id and value.get("memory_epoch") == state.get("memory_epoch"):
                    self.outcomes.append(value["outcome"])

    def _reserve(self, effect):
        with self.db_session() as db:
            owner(db, self.snapshot["id"], self.turn_id)
            identifier = operation_id(self.snapshot["id"], self.request.request_id, "input")
            message = db.get(Message, identifier)
            if message is None:
                raise ValueError("委派缺少正式输入回执")
            budget = dict(message.metadata_json.get("_native_budget", {}))
            reserved = list(budget.get("reserved", []))
            if effect not in reserved:
                if len(reserved) >= MAX_DELEGATIONS:
                    raise ValueError("本次请求的子 Agent 总预算已到")
                reserved.append(effect)
                message.metadata_json = {**message.metadata_json, "_native_budget": {"reserved": reserved}}
            self.calls = len(reserved)

    def session(self):
        with self.db_session() as db:
            row = db.get(LearningSession, self.snapshot["id"])
            if (
                row is None
                or row.course_id != self.state["course_id"]
                or row.student_id != self.state["student_id"]
                or row.active_turn_id != self.turn_id
            ):
                raise LearningTurnLost("子任务不再持有当前学习会话")
            return {
                "pending": bool(row.pending_question_id),
                "pending_question_id": row.pending_question_id,
                "version": row.version,
                "context": dict(row.pending_context),
            }

    def assistance_owner(self, db, context):
        """帮助条件影响评分，必须在同一写事务中锁定执行权与撤销版本。"""
        row = owner(db, context.session_id, self.turn_id)
        if (row.course_id, row.student_id) != (context.course_id, context.student_id):
            raise LearningTurnLost("帮助标记不再属于当前学习范围")
        epoch = context.memory_epoch if context.memory_epoch is not None else self.state.get("memory_epoch")
        if epoch is not None and invalidation_epoch(db, row.student_id, lock=True) != epoch:
            raise LearningConflict("学习记忆已撤销，不能提交旧的帮助条件")
        return row

    async def __call__(self, role, name, arguments, context, *, runtime_context=None, config=None):
        invocation = runtime_context
        if invocation is None or invocation.role != role or invocation.depth >= 2:
            raise PermissionError("子 Agent 调用链无效或超过两层委派深度")
        self.session()
        if (context.course_id, context.session_id, context.student_id) != (
            self.state["course_id"],
            self.snapshot["id"],
            self.state["student_id"],
        ):
            raise PermissionError("子 Agent 不能改变课程、会话或学生身份")
        ids = list(dict.fromkeys(arguments["concept_ids"]))
        resolved = self.learning.catalog.resolve_concepts(
            "", context.course_id, ids
        )
        if any(c["topic_type"] == "category" for c in resolved):
            raise ValueError("请先选择可教学知识点，不能把分类目录当作教学目标")
        if role == "knowledge" and not set(ids).issubset(context.concept_ids):
            if arguments.get(
                "operation"
            ) != "explain" or not self.authorized_example(arguments, context):
                raise PermissionError(
                    "知识点子 Agent 不能把习题任务扩展到教学范围之外"
                )
        value = {"role": role, "name": name, "arguments": arguments}
        key = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        effect = operation_id(self.snapshot["id"], self.request.request_id, key)
        for outcome in self.outcomes:
            if outcome["key"] == key:
                self.consumed.setdefault(invocation.run_id, set()).add(key)
                return {**outcome["result"], "replayed": True}
        self._reserve(effect)
        child = replace(context, concept_ids=ids)
        if name == "call_knowledge_agent":
            result = await self.knowledge(arguments, child, runtime_context=invocation, config=config)
        else:
            result = await self.exercise(arguments, child, runtime_context=invocation, config=config, operation_id=effect)
        self.session()
        outcome = {**value, "key": key, "result": result}
        with self.db_session() as db:
            owner(db, self.snapshot["id"], self.turn_id)
            epoch = self.state["memory_epoch"]
            if invalidation_epoch(db, self.state["student_id"], lock=True) != epoch:
                raise ValueError("学习记忆已撤销，不能接受旧的子任务候选")
            # 正式业务操作身份保持稳定，私有候选随撤销版本分别留存，便于故障后回放当前结果。
            identifier = operation_id(self.snapshot["id"], effect, f"delegation:{epoch}")
            if db.get(Message, identifier) is None:
                db.add(Message(id=identifier, session_id=self.snapshot["id"], role="assistant", agent=role,
                    content="已接受子任务结果。", citations=[], metadata_json={"record_only": True,
                    "_delegation_receipt": {"request_id": self.request.request_id, "memory_epoch": self.state.get("memory_epoch"), "outcome": outcome}}))
        self.outcomes.append(outcome)
        self.consumed.setdefault(invocation.run_id, set()).add(key)
        await self.emit("agent_returned", {"caller": role, "agent": result["agent"],
            "parent_run_id": invocation.run_id, "child_run_ids": result["child_run_ids"], "status": result["status"]})
        return result

    async def knowledge(self, arguments, context, *, runtime_context=None, config=None):
        learning = self.learning
        current = self.session()
        pending = current["pending"]
        own_batch = current["context"].get("exercise_batch", {}).get("id")
        restoring_own_batch = own_batch and any(
            o["result"].get("exercise_batch", {}).get("id") == own_batch
            for o in self.outcomes
        )
        if pending and not restoring_own_batch:
            with self.db_session() as db:
                row = self.assistance_owner(db, context)
                if (row.version != current["version"] or row.pending_question_id != current["pending_question_id"]
                    or row.pending_context != current["context"]):
                    raise LearningTurnLost("待答题已变化，不能提交旧的帮助条件")
                row.pending_context = mark_batch_assisted(row.pending_context)
        with self.db_session() as db:
            exercise_context = SessionExerciseService(
                db, context.session_id, context.course_id, context.student_id
            ).context()
        # 授权具体的冻结例题，而非把不同名称的标签当作不相关教学任务。
        example_ids = [
            exercise["exercise_id"]
            for key in ("active_exercise", "pending_exercise")
            if (exercise := exercise_context.get(key)) and exercise.get("exercise_id")
        ]
        context = replace(
            context, explanation_exercise_ids=list(dict.fromkeys(example_ids))
        )

        def validate(output):
            spec = output.get("exercise_batch")
            if spec:
                validate_batch_request(spec, context)
                consumed = set().union(*self.consumed.values()) if self.consumed else set()
                if not any(
                    o["key"] in consumed
                    and o["name"] == "call_exercise_agent"
                    and o["arguments"]["operation"] == "batch"
                    for o in self.outcomes
                ):
                    raise ValueError(
                        "练习组必须先通过 call_exercise_agent 工具执行并读取返回结果，不能只写出题计划"
                    )

        result = await learning.runner.run(
            "knowledge",
            "auto",
            {
                "goal": self.state["goal"],
                "question": self.request.content,
                "teaching_request": arguments["task"],
                "learning_plan": self.snapshot["agenda"],
                "exercise_context": exercise_context,
                "can_request_exercise_batch": not pending,
                "completed_delegations": self.outcomes,
                "exercise_preferences": {
                    "count": self.request.exercise_count,
                    "cognitive_type": (
                        self.request.cognitive_type
                        if "cognitive_type" in self.request.model_fields_set
                        else None
                    ),
                },
                "concepts": [
                    {"id": c["id"], "name": c["name"]}
                    for c in learning.catalog.resolve_concepts(
                        "", context.course_id, context.concept_ids
                    )
                ],
                "student_state": learning.student.profile(
                    context.course_id, context.concept_ids, context.student_id
                )["states"][:6],
                "learning_memory": learning.student.memory(
                    context.course_id, context.concept_ids, context.student_id
                ),
                "recent_messages": [
                    {"role": m["role"], "content": m["content"]}
                    for m in self.snapshot["messages"][-12:]
                ],
                "instruction": "完成受委派教学并把结果交回主Agent。需要习题讲解、出题或练习组时，调用call_exercise_agent并等待结果，然后继续完善本次教学；不要只写exercise_batch或next_action而不执行。已有待答练习不得替换。你的正文会由主Agent统一展示，不要宣称长期学习目标已经完成。",
            },
            Lesson,
            context,
            self.turn_id,
            emit=self.emit,
            tool_source=self,
            runtime_context=runtime_context,
            config=config,
            validate_output=validate,
            timeout_seconds=480,
        )
        lesson = result["output"]
        return {
            "agent": "knowledge",
            "status": "completed",
            "lesson": lesson,
            "citations": result["citations"],
            "child_run_ids": [result["run_id"]],
            "pending_student_answer": self.session()["pending"],
        }

    async def exercise(self, arguments, context, *, runtime_context=None, config=None, operation_id=None):
        learning, operation = self.learning, arguments["operation"]
        spec = arguments.get("exercise_batch")
        requirements = (
            validate_batch_request(spec, context) if spec is not None else None
        )
        validate_generation_operation(
            operation, len(requirements.tasks) if requirements is not None else None
        )
        if operation == "explain":
            if runtime_context.role == "knowledge" and not self.authorized_example(
                arguments, context
            ):
                raise PermissionError("只能讲解父级提供或本次子任务已准备的具体例题")
            with self.db_session() as db:
                row = self.assistance_owner(db, context)
                history = SessionExerciseService(
                    db, context.session_id, context.course_id, context.student_id
                )
                contract = history.internal_contract(arguments.get("exercise_id"))
                batch = row.pending_context.get("exercise_batch")
                if row.pending_question_id == contract["id"] or (
                    batch
                    and any(
                        item["contract"]["id"] == contract["id"]
                        for item in batch["items"]
                    )
                ):
                    row.pending_context = mark_batch_assisted(
                        row.pending_context, contract["id"]
                    )
            context = replace(
                context,
                concept_ids=list(contract["concept_ids"]),
                excluded_question_ids=list(
                    set(context.excluded_question_ids + [contract["id"]])
                ),
            )
            result = await learning.exercises.explain(
                contract,
                context,
                self.turn_id,
                self.emit,
                user_question=arguments.get("task") or self.request.content,
                runtime_context=runtime_context, config=config,
            )
            return {
                "agent": "exercise",
                "operation": operation,
                "status": "completed",
                "explanation": result["output"],
                "citations": result["citations"],
                "child_run_ids": [result["run_id"]],
            }
        # 正式发题已完成而父工具回执丢失时，先回放原题；不能因 pending 拒绝恢复。
        with self.db_session() as db:
            published = next((m for m in db.scalars(select(Message).where(Message.session_id == context.session_id))
                              if (m.metadata_json or {}).get("_operation_id") == operation_id), None)
            if published:
                return {"agent": "exercise", "operation": operation, "status": "awaiting_student",
                        "question": published.metadata_json["question"], "exercise_id": published.id,
                        "citations": [], "child_run_ids": [], "replayed": True}
        if self.session()["pending"] and operation != "batch":
            raise ValueError("已有等待作答的练习，请先完成或跳过，不能替换")
        teaching_request = arguments.get("task") or self.request.content
        if requirements is not None:
            if (
                self.request.exercise_count
                and len(requirements.tasks) != self.request.exercise_count
            ):
                raise ValueError("练习题数必须遵守学生明确要求")
            if "cognitive_type" in self.request.model_fields_set and any(
                t.cognitive_type != self.request.cognitive_type
                for t in requirements.tasks
            ):
                raise ValueError("练习组认知类型必须遵守学生明确要求")
        if operation == "batch":
            run_ids = []

            async def capture(event, data):
                if event == "agent_started":
                    run_ids.append(data["run_id"])
                await self.emit(event, data)

            result, citations = await learning.batches.generate(
                requirements.model_dump(mode="json"),
                context,
                self.turn_id,
                capture,
                teaching_request=teaching_request,
                operation_id=operation_id, config=config, runtime_context=runtime_context,
            )
            return {
                "agent": "exercise",
                "operation": operation,
                "status": "awaiting_student",
                **result,
                "citations": citations,
                "child_run_ids": run_ids,
            }
        if (self.request.exercise_count or 1) > 1:
            raise ValueError("学生明确要求多题，请使用batch操作并指定完整练习组")
        task = requirements.tasks[0] if requirements is not None else None
        cognitive = (
            task.cognitive_type
            if task
            else arguments.get("cognitive_type", self.request.cognitive_type)
        )
        if (
            "cognitive_type" in self.request.model_fields_set
            and cognitive != self.request.cognitive_type
        ):
            raise ValueError("练习认知类型必须遵守学生明确要求")
        if task is not None:
            # 单题同样收紧到逐题计划的范围，不把其他知识点带入生成器。
            context = replace(context, concept_ids=list(task.concept_ids))
        run_ids = []

        async def capture(event, data):
            if event == "agent_started":
                run_ids.append(data["run_id"])
            await self.emit(event, data)

        contract, selected = await (
            learning.exercises.generate(
                context,
                self.turn_id,
                cognitive,
                capture,
                operation_id=operation_id, config=config, runtime_context=runtime_context,
                assignment=generation_assignment(
                    task, teaching_request=teaching_request
                ),
            )
            if operation == "generate"
            else learning.exercises.select(context, self.turn_id, cognitive, capture, runtime_context=runtime_context, config=config)
        )
        result = await learning.pose_contract(
            context.session_id, contract, context, self.turn_id, cognitive, capture, operation_id=operation_id
        )
        return {
            "agent": "exercise",
            "operation": operation,
            "status": "awaiting_student",
            **result,
            "citations": selected["citations"],
            "child_run_ids": list(dict.fromkeys([*run_ids, selected["run_id"]])),
        }

    def authorized_example(self, arguments, context):
        """对父级提供的精确出题记录授权，不能借标签关系读取任意历史习题。"""
        with self.db_session() as db:
            history = SessionExerciseService(
                db, context.session_id, context.course_id, context.student_id
            )
            exercise = history.get(arguments.get("exercise_id"))
            if not exercise:
                return False
            allowed = list(context.explanation_exercise_ids)
            allowed.extend(
                o["result"]["exercise_id"]
                for o in self.outcomes
                if o["role"] == "knowledge" and o["result"].get("exercise_id")
            )
            return exercise["exercise_id"] in allowed

    def validate_final(self, plan):
        if plan["action"] in {"teach", "review"} and not any(
            o["name"] == "call_knowledge_agent" for o in self.outcomes
        ):
            raise ValueError(
                "请先调用call_knowledge_agent并读取返回结果，再完成本轮答复"
            )
        if plan["action"] == "explain" and not any(
            o["arguments"].get("operation") == "explain" for o in self.outcomes
        ):
            raise ValueError(
                "请先调用call_exercise_agent的explain操作，题解完成后继续主Agent决策"
            )
        if plan["action"] == "practice" and not self.session()["pending"]:
            raise ValueError(
                "请先通过call_exercise_agent实际准备练习，不能仅返回出题建议"
            )
        if (self.outcomes or self.require_continuation) and not plan.get(
            "continuation"
        ):
            raise ValueError(
                "子Agent已返回；主Agent必须给出continuation，交代长期目标当前进度和下一步或学生等待点"
            )
        if (
            self.session()["pending"]
            and (plan.get("continuation") or {}).get("status") == "completed"
        ):
            raise ValueError("仍有待答练习，长期任务不能标为完成")
        if (
            self.request.exercise_count
            and not self.session()["pending"]
            and self.outcomes
            and self.request.intent != "explain"
        ):
            raise ValueError("尚未完成学生明确要求的练习出题，请继续调用习题工具")

    def presentation(self, plan):
        return present_delegations(self.outcomes, self.request.request_id, plan)
