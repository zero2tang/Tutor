"""习题角色负责选题、生成及题解；服务负责冻结与保存题目。"""

from sqlalchemy import select

from app.infrastructure.persistence.database import db_session
from app.domains.assessment.models import Attempt
from app.domains.courses.models import Question
from app.infrastructure.persistence.execution_models import AgentRun
from app.domains.practice.operations import operation_key, require_owner
from app.domains.sessions.interactions import digest as fingerprint
from app.shared.identity import operation_id as stable_id
from app.agent.runtime.contracts import AgentRunner, EventSink
from app.domains.practice.schemas import (
    ExerciseDraft,
    ExerciseSelection,
    Explanation,
    SolutionCheck,
)
from app.domains.courses.catalog import CatalogService, question_contract
from app.domains.courses.questions.policy import can_practice, require_access
from app.domains.practice.reviews import (
    ACCEPTED_VERDICTS,
    ExerciseReviewService,
    draft_issue,
    record_generation_attempt,
)
from app.agent.tools.contracts import ToolContext
from app.shared.execution import LearningConflict


class PracticeService:
    def __init__(self, runner: AgentRunner, *, generation_graph=None):
        """复用 Agent 执行器和课程目录，统一选题、生成与题解的事实来源。"""
        self.runner = runner
        self.generation_graph = generation_graph
        self.catalog = CatalogService()
        self.reviews = ExerciseReviewService(runner)

    async def select(
        self,
        context: ToolContext,
        turn_id: str,
        cognitive_type: str,
        emit: EventSink,
        explicit_question_id: str | None = None,
        *, config=None, runtime_context=None,
    ) -> tuple[dict, dict]:
        """检索可用候选题，再由习题 Agent 从候选集中选择一题。

        排除本会话已答题，优先当前知识点，无候选时扩大到同一课程。
        显式题号仍需通过课程与可用性校验；只有选题完成后才读取含答案的契约。
        """
        self.catalog.course(context.course_id)
        for cid in context.concept_ids:
            self.catalog.concept(cid, context.course_id)
        with db_session() as db:
            seen = [
                a.question_id
                for a in db.scalars(
                    select(Attempt).where(Attempt.session_id == context.session_id)
                )
            ]
        candidates = await self.runner.tools.retrieval.retrieve(
            "",
            course_id=context.course_id,
            concept_ids=context.concept_ids,
            kind="questions",
            mode="keyword",
            top_k=12,
            excluded_question_ids=seen,
        )
        questions = [
            item["question"]
            for item in candidates["items"]
            if can_practice(item["question"])
        ]
        if explicit_question_id:
            selected = self.catalog.question(explicit_question_id, context.course_id)
            require_access(selected, "practice")
            questions = [selected]
        if not questions:
            candidates = await self.runner.tools.retrieval.retrieve(
                "",
                course_id=context.course_id,
                kind="questions",
                mode="keyword",
                top_k=8,
                excluded_question_ids=seen,
            )
            questions = [
                item["question"]
                for item in candidates["items"]
                if can_practice(item["question"])
            ]
        if not questions:
            raise ValueError("当前没有未练习的题目，可以请求生成新题")
        result = await self.runner.run(
            "exercise",
            cognitive_type,
            {
                "task": "select",
                "target_concepts": context.concept_ids,
                "cognitive_type": cognitive_type,
                "candidates": questions,
                "instruction": "选择一题，question_id 必须原样来自 candidates。原始认知标签 unknown 不能伪装为已标注。",
            },
            ExerciseSelection,
            context,
            turn_id,
            emit=emit,
            config=config,
            runtime_context=runtime_context,
        )
        identifier = result["output"]["question_id"]
        if identifier not in {q["id"] for q in questions}:
            raise ValueError("习题 Agent 选择了候选集外的题目")
        return (
            self.catalog.question(identifier, context.course_id, internal=True),
            result,
        )

    async def generate(
        self,
        context: ToolContext,
        turn_id: str,
        cognitive_type: str,
        emit: EventSink,
        *,
        assignment: dict | None = None,
        operation_id: str | None = None,
        task_id: str = "single",
        config=None,
        runtime_context=None,
    ) -> tuple[dict, dict]:
        """单题与整组共用质量子图；稳定身份保证返回丢失后不重复生成。"""
        if not context.concept_ids:
            raise ValueError("生成习题前需要先确定知识点")
        self.catalog.course(context.course_id)
        for cid in context.concept_ids:
            self.catalog.concept(cid, context.course_id)
        requirements = assignment or {}
        desired_type = requirements.get("question_type", "choice")
        if desired_type not in {"choice", "multi_choice", "fill"}:
            raise ValueError("生成题型不受支持")
        key = operation_key(context, operation_id, "practice_generate")
        state = {
            "operation_id": key,
            "task_id": task_id,
            "question_id": context.course_id + ":g:" + stable_id(context.session_id, key, task_id).replace("-", ""),
            "input_hash": fingerprint({
                "session_id": context.session_id,
                "student_id": context.student_id,
                "course_id": context.course_id,
                "concept_ids": context.concept_ids,
                "cognitive_type": cognitive_type,
                "assignment": requirements,
            }),
            "cognitive_type": cognitive_type,
            "assignment": requirements,
            "attempt_number": 0,
            "last_issue": "",
            "previous_attempt": None,
        }
        with db_session() as db:
            state["session_version"] = require_owner(db, context, turn_id).version
        prior = self.existing_generation(state, context, turn_id)
        if prior:
            return prior["contract"], prior["result"]
        self.restore_generation_budget(state, context, turn_id)
        if self.generation_graph is None:
            raise RuntimeError("应用未装配统一习题生成图")
        # 图的服务对象放在运行上下文，检查点只保存可序列化的题目候选和结果。
        result = await self.generation_graph.ainvoke(
            state,
            config=config,
            context={"exercises": self, "context": context, "turn_id": turn_id, "emit": emit,
                     "runtime_context": runtime_context},
            **({"durability": "sync"} if config and config.get("metadata", {}).get("tutor_durable") else {}),
        )
        return result["contract"], result["result"]

    def restore_generation_budget(self, state, context, turn_id):
        """仅复用既有诊断记录，失败后换请求执行 owner 不能重置两次质量预算。"""
        with db_session() as db:
            require_owner(db, context, turn_id)
            runs = db.scalars(select(AgentRun).where(
                AgentRun.session_id == context.session_id,
                AgentRun.private_diagnostics["generation_attempt"]["operation_id"].as_string() == state["operation_id"],
                AgentRun.private_diagnostics["generation_attempt"]["task_id"].as_string() == state["task_id"],
            )).all()
            records = [run.private_diagnostics["generation_attempt"] for run in runs]
            if not records:
                return
            if any(record.get("input_hash") != state["input_hash"] for record in records):
                raise ValueError("同一生成操作标识不能对应不同输入")
            latest = max(records, key=lambda record: record["attempt_number"])
            details = latest["details"]
            state["attempt_number"] = latest["attempt_number"]
            state["last_issue"] = latest["reason"]
            state["previous_attempt"] = {
                "question": details["generation"]["output"],
                "independent_solution": (details.get("independent") or {}).get("output"),
                "review": (details.get("review") or {}).get("output"),
                "format_issue": latest["reason"],
            }

    def existing_generation(self, state, context, turn_id):
        """回执优先于重新执行，严格限定到同一学生、课程、会话及输入。"""
        with db_session() as db:
            require_owner(db, context, turn_id)
            row = db.get(Question, state["question_id"])
            if row is None:
                return None
            receipt = (row.validation or {}).get("generation_operation", {})
            if (
                receipt.get("operation_id") != state["operation_id"]
                or receipt.get("task_id") != state["task_id"]
                or receipt.get("input_hash") != state["input_hash"]
            ):
                raise ValueError("同一生成操作标识不能对应不同输入")
            if context.memory_epoch is not None and receipt.get("memory_epoch") != context.memory_epoch:
                raise LearningConflict("未发布的生成候选使用了已撤销的记忆，请使用新请求重新生成；原产物已保留。")
            return {"contract": receipt["contract"], "result": receipt["result"]}

    async def draft_generation(self, state, run, *, config=None):
        """格式失败直接重做草稿，不浪费独立求解和复核预算。"""
        prior = self.existing_generation(state, run.context, run.turn_id)
        if prior:
            return prior
        if state["attempt_number"] >= 2:
            from app.agent.tools.errors import ToolRejected

            raise ToolRejected("生成题在预算内未通过质量检查：" + state["last_issue"])
        with db_session() as db:
            require_owner(db, run.context, run.turn_id, expected_version=state["session_version"])
        assignment = state["assignment"]
        attempt_number = state["attempt_number"] + 1
        result = await self.runner.run(
            "exercise",
            state["cognitive_type"],
            {
                "task": "generate",
                "concepts": [
                    self.catalog.concept(cid, run.context.course_id)
                    for cid in run.context.concept_ids[:3]
                ],
                "cognitive_type": state["cognitive_type"],
                "question_type": assignment.get("question_type", "choice"),
                "previous_issue": state["last_issue"],
                "previous_attempt": state["previous_attempt"],
                "assignment": assignment,
                "instruction": "生成一道条件充分的原创题，遵守question_type及assignment中的instruction、teaching_request和其他要求。choice为四选一；multi_choice为四选多，至少两个正确选项；fill仅一个填空、options为空、answer只含一个标准答案。先完成explanation中的解答和代回检验，再填写与结论一致的answer；选择题先求结果、核对选项后填写标签。若指定difficulty_range，以0至100内容认知负担量表设计，但不要自称已通过难度验证。",
            },
            ExerciseDraft,
            run.context,
            run.turn_id,
            emit=run.emit,
            runtime_context=run.runtime_context,
            config=config,
        )
        # 子 Agent 的消息由其原生图拥有，父质量图只保存业务候选和调用回执。
        result.pop("messages", None)
        issue = draft_issue(
            result["output"], assignment.get("question_type", "choice"),
            state["cognitive_type"], run.context.concept_ids,
        )
        if issue:
            with db_session() as db:
                require_owner(db, run.context, run.turn_id, expected_version=state["session_version"])
                audit_id = record_generation_attempt(
                    db, run.context, run.turn_id, assignment, attempt_number,
                    result, None, None, issue,
                    operation={key: state[key] for key in ("operation_id", "task_id", "input_hash")},
                )
            await run.emit("exercise_quality_checked", {
                "stage": "format", "status": "rejected", "attempt": attempt_number,
                "task_index": assignment.get("index"), "audit_id": audit_id,
            })
        return {
            "attempt_number": attempt_number,
            "draft_result": result,
            "last_issue": issue,
            "previous_attempt": (
                {"question": result["output"], "format_issue": issue}
                if issue else state["previous_attempt"]
            ),
        }

    async def solve_generation(self, state, run, *, config=None):
        """只给盲解模型题干和选项，不传生成答案及生成解析。"""
        draft = state["draft_result"]["output"]
        with db_session() as db:
            require_owner(db, run.context, run.turn_id, expected_version=state["session_version"])
        check = await self.runner.run(
            "exercise",
            state["cognitive_type"],
            {
                "task": "validate",
                "stem": draft["stem"],
                "options": draft["options"],
                "question_type": draft["question_type"],
                "concept_ids": draft["concept_ids"],
                "instruction": "独立求解并判断唯一可解性，不知道生成者参考答案。先在explanation中完成解答和代回检验，再填写与结论一致的answer；选择题核对结果对应的选项标签。数量域依据题干和对象判断：人数、书本数等为整数，重量、长度等连续量允许分数；不得添加题目没有要求的整数限制。",
            },
            SolutionCheck,
            run.context,
            run.turn_id,
            emit=run.emit,
            use_tools=False,
            runtime_context=run.runtime_context,
            config=config,
        )
        check.pop("messages", None)
        return {"independent_result": check}

    async def review_generation(self, state, run, *, config=None):
        """接受后的题目与重放回执在同一事务中落库。"""
        prior = self.existing_generation(state, run.context, run.turn_id)
        if prior:
            return prior
        with db_session() as db:
            require_owner(db, run.context, run.turn_id, expected_version=state["session_version"])
        result = state["draft_result"]
        draft = result["output"]
        check = state["independent_result"]
        solved = check["output"]
        review_result = await self.reviews.reconcile(
            draft, solved, run.context, run.turn_id, run.emit,
            runtime_context=run.runtime_context, config=config,
        )
        review = review_result["output"]
        accepted = review["verdict"] in ACCEPTED_VERDICTS
        with db_session() as db:
            require_owner(db, run.context, run.turn_id, expected_version=state["session_version"])
            audit_id = record_generation_attempt(
                db, run.context, run.turn_id, state["assignment"],
                state["attempt_number"], result, check, review_result, "",
                operation={key: state[key] for key in ("operation_id", "task_id", "input_hash")},
            )
            if accepted:
                # 唯一题号使并发或恢复只能复用同一正式产物。
                row = db.get(Question, state["question_id"])
                if row is not None:
                    receipt = (row.validation or {}).get("generation_operation", {})
                    if receipt.get("input_hash") != state["input_hash"]:
                        raise ValueError("同一生成操作标识不能对应不同输入")
                    if run.context.memory_epoch is not None and receipt.get("memory_epoch") != run.context.memory_epoch:
                        raise LearningConflict("未发布的生成候选使用了已撤销的记忆，请使用新请求重新生成；原产物已保留。")
                    contract, result = receipt["contract"], receipt["result"]
                else:
                    row = Question(
                        id=state["question_id"],
                        course_id=run.context.course_id,
                        source_id="generated:" + state["question_id"][-32:],
                        stem=draft["stem"], options=draft["options"],
                        answer=review["answer"], question_type=draft["question_type"],
                        cognitive_type=state["cognitive_type"],
                        concept_ids=draft["concept_ids"], explanation=review["explanation"],
                        source_ref="习题 Agent 生成；经独立求解与模型语义复核",
                        generated=True,
                        validation={
                            "quality_method": "semantic_reconciliation_v2",
                            "semantic_review_passed": True,
                            "review_verdict": review["verdict"],
                            "generation_run": result["run_id"],
                            "validation_run": check["run_id"],
                            "review_run": review_result["run_id"],
                            "generation_attempt_id": audit_id,
                            "expert_verified": False,
                            "generation_requirements": state["assignment"],
                            "difficulty_target": state["assignment"].get("difficulty_range"),
                            "generation_operation": {
                                "operation_id": state["operation_id"],
                                "task_id": state["task_id"],
                                "input_hash": state["input_hash"],
                                "memory_epoch": run.context.memory_epoch,
                                "result": {
                                    key: result[key]
                                    for key in ("run_id", "citations", "usage", "duration_ms")
                                    if key in result
                                },
                            },
                            **({"blank_count": 1} if draft["question_type"] == "fill" else {}),
                        },
                    )
                    db.add(row)
                    db.flush()
                    contract = question_contract(row)
                    receipt = {**row.validation["generation_operation"], "contract": contract}
                    row.validation = {**row.validation, "generation_operation": receipt}
                    result = receipt["result"]
        await run.emit("exercise_quality_checked", {
            "stage": "semantic_review", "status": "accepted" if accepted else "rejected",
            "verdict": review["verdict"], "attempt": state["attempt_number"],
            "task_index": state["assignment"].get("index"), "audit_id": audit_id,
            "review_run_id": review_result["run_id"],
        })
        if accepted:
            return {"contract": contract, "result": result}
        return {
            "last_issue": "语义复核未通过：" + review["reason"],
            "previous_attempt": {
                "question": {"stem": draft["stem"], "options": draft["options"]},
                "generation_solution": {"answer": draft["answer"], "explanation": draft["explanation"]},
                "independent_solution": solved, "review": review,
            },
        }

    async def explain(
        self,
        contract: dict,
        context: ToolContext,
        turn_id: str,
        emit: EventSink,
        *,
        user_question: str = "",
        config=None,
        runtime_context=None,
    ) -> dict:
        """基于内部标准答案生成分步题解；待答题的辅助标记由上层编排先行提交。"""
        if contract.get("course_id") != context.course_id:
            raise ValueError("题目契约不属于当前课程")
        # 解析请求来自已授权教学用例；历史冻结题可保留原解析，不能借此进入共享检索。
        require_access(contract, "solution", solution_authorized=True, allow_archived_solution=True)
        return await self.runner.run(
            "exercise",
            "understanding",
            {
                "task": "explain",
                "question_contract": contract,
                "user_question": user_question,
                "instruction": "围绕用户对这道题的具体追问，根据冻结题目及标准答案讲解；用户只问某一步时重点解释该步骤，并说明关键条件。",
            },
            Explanation,
            context,
            turn_id,
            emit=emit,
            config=config,
            runtime_context=runtime_context,
        )
