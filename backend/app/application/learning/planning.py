"""为主 Agent 提供真实上下文；不预判用户意图、不自动把原句送去检索。"""

from app.infrastructure.persistence.database import db_session
from app.domains.sessions.models import LearningSession
from app.domains.learning.schemas import Plan
from app.domains.courses.catalog import CatalogService
from app.domains.practice.context import SessionExerciseService
from app.agent.tools.contracts import ToolContext
from dataclasses import replace
from app.domains.learning.progress import save_progress
from app.shared.execution import LearningTurnLost
from app.domains.sessions.goals import (
    apply_goal_update,
    build_goal_candidate,
    context_projection,
    ensure_session_goals,
)


class LearningPlanner:
    def __init__(self, runner):
        """复用角色执行器，并通过目录服务校验模型选择的教学目标。"""
        self.runner = runner
        self.catalog = CatalogService()

    def validate(
        self,
        plan: dict,
        course_id: str,
        has_exercise: bool = False,
        context: ToolContext | None = None,
        effects_applied: bool = False,
    ) -> None:
        """校验计划是否能执行：题解必须有题，教学目标必须属于本课且不是目录节点。"""
        if (
            plan["action"] == "explain"
            and not has_exercise
            and not plan.get("exercise_id")
        ):
            raise ValueError(
                "当前没有可讲解的习题。explain 专指习题题解；如果用户要解释知识点、原理或概念，应选择 teach 并查询具体知识点。"
            )
        if context and (plan.get("exercise_id") or plan["action"] == "explain"):
            with db_session() as db:
                history = SessionExerciseService(
                    db, context.session_id, course_id, context.student_id
                )
                if plan["action"] == "explain":
                    history.internal_contract(plan.get("exercise_id"))
                else:
                    history.get(plan["exercise_id"])
        if plan.get("exercise_batch"):
            if plan["action"] not in {"practice", "teach", "review"}:
                raise ValueError("只有练习或教学动作可以安排练习组")
            if context:
                from app.domains.practice.batches import validate_batch_request

                validate_batch_request(
                    plan["exercise_batch"],
                    replace(context, concept_ids=list(plan["concept_ids"])),
                )
                with db_session() as db:
                    if (
                        not effects_applied
                        and db.get(
                            LearningSession, context.session_id
                        ).pending_question_id
                    ):
                        raise ValueError("已有未完成练习，请先完成或跳过，不得替换")
        if not plan["concept_ids"]:
            return
        concepts = self.catalog.resolve_concepts("", course_id, plan["concept_ids"])
        if plan["action"] in {"teach", "practice", "review"} and any(
            c["topic_type"] == "category" for c in concepts
        ):
            raise ValueError(
                "所选目标包含分类目录。请用 search_concepts 查找具体知识点，或用 respond 介绍目录，不能直接作为知识教学目标。"
            )

    async def plan(
        self,
        session_id,
        state,
        snapshot,
        request,
        turn_id,
        emit,
        *,
        tool_source=None,
        output_validator=None,
        effects_applied=None,
        extra_payload=None,
        config=None,
        messages=None,
    ):
        """把会话事实交给主 Agent，返回合法计划及供后续角色复用的 ToolContext。

        payload 是模型可见的目标、消息和状态摘要；context 保存工具执行时的
        课程、学生及排除题号。先验证模型输出，再更新目标范围并持久化计划。
        """
        current = self.catalog.resolve_concepts(
            "", state["course_id"], requested=state["concept_ids"]
        )
        explicit = (
            self.catalog.resolve_concepts(
                "", state["course_id"], requested=request.concept_ids
            )
            if request.concept_ids
            else []
        )
        focused = [
            c["id"] for c in explicit or current if c["topic_type"] != "category"
        ]
        with db_session() as db:
            session_goals = context_projection(
                ensure_session_goals(db, db.get(LearningSession, session_id))
            )
            exercise_context = SessionExerciseService(
                db, session_id, state["course_id"], state["student_id"]
            ).context()
        has_exercise = bool(exercise_context["active_exercise"])
        context = ToolContext(
            course_id=state["course_id"],
            session_id=session_id,
            concept_ids=focused,
            excluded_question_ids=(
                [state["pending_question_id"]] if state["pending_question_id"] else []
            ),
            student_id=state["student_id"],
            request_id=request.request_id,
            memory_epoch=state.get("memory_epoch"),
        )

        def validate_output(plan):
            with db_session() as db:
                session = db.get(LearningSession, session_id)
                owner = session.active_turn_id if session else None
                if owner == turn_id and plan.get("session_goal_update"):
                    if plan.get("goal_update") and plan["goal_update"] != session.goal:
                        raise ValueError(
                            "更换整体目标时session_goal_update应为空，读取新根后再分解"
                        )
                    build_goal_candidate(
                        db,
                        session,
                        ensure_session_goals(db, session),
                        plan["session_goal_update"],
                        continuation=plan.get("continuation"),
                    )
            if owner != turn_id:
                raise LearningTurnLost(
                    "主Agent已失去当前会话，停止旧任务而不是继续修复输出"
                )
            if plan.get("goal_update") and plan["goal_update"] != state["goal"]:
                quote = plan.get("goal_update_request")
                if (
                    not quote
                    or quote not in request.content
                    or (extra_payload or {}).get("assessment_result")
                ):
                    raise ValueError(
                        "更换整体学习目标必须提供当前用户明确提出更换要求的原话goal_update_request。阶段推进或改写措辞时请保持goal_update=null，把进度写入continuation。"
                    )
            self.validate(
                plan,
                state["course_id"],
                has_exercise,
                context,
                effects_applied=bool(effects_applied and effects_applied()),
            )
            if output_validator:
                output_validator(plan)
            if (
                plan["action"] == "practice"
                and (request.exercise_count or 1) > 1
                and not plan.get("exercise_batch")
            ):
                raise ValueError(
                    "用户已明确要求多题，请用exercise_batch逐题安排知识点、类型与考查要求"
                )
            if (
                request.exercise_count
                and plan.get("exercise_batch")
                and len(plan["exercise_batch"]["tasks"]) != request.exercise_count
            ):
                raise ValueError("练习数量必须遵守用户明确指定的题数")
            if (
                "cognitive_type" in request.model_fields_set
                and plan.get("exercise_batch")
                and any(
                    task["cognitive_type"] != request.cognitive_type
                    for task in plan["exercise_batch"]["tasks"]
                )
            ):
                raise ValueError("练习认知类型必须遵守用户明确指定的要求")

        result = await self.runner.run(
            "supervisor",
            "auto",
            {
                "user_message": request.content,
                "requested_operation": request.intent,
                "requested_generate": request.generate,
                "requested_exercise_count": request.exercise_count,
                "requested_cognitive_type": (
                    request.cognitive_type
                    if "cognitive_type" in request.model_fields_set
                    else None
                ),
                "goal": state["goal"],
                "learning_plan": snapshot["agenda"],
                "learning_progress": state["pending_context"].get("learning_progress"),
                "session_goals": session_goals,
                "course": self.catalog.course(state["course_id"]),
                "current_concepts": [
                    {"id": c["id"], "name": c["name"], "topic_type": c["topic_type"]}
                    for c in current
                ],
                "explicit_concepts": [
                    {"id": c["id"], "name": c["name"]} for c in explicit
                ],
                "pending_question": bool(state["pending_question_id"]),
                "has_exercise_to_explain": has_exercise,
                "pending_decision": state["pending_context"].get("decision"),
                "exercise_context": exercise_context,
                "recent_messages": [
                    {"id": m["id"], "role": m["role"], "content": m["content"]}
                    for m in snapshot["messages"]
                ],
                **(extra_payload or {}),
            },
            Plan,
            context,
            turn_id,
            emit=emit,
            validate_output=validate_output,
            tool_source=tool_source,
            config=config,
            messages=messages,
            timeout_seconds=600 if tool_source else 240,
        )
        # 校验回调可在工具循环内让模型修正计划；保存前再检查一次执行约束。
        plan = result["output"]
        validate_output(plan)
        if plan["concept_ids"]:
            context.concept_ids = list(plan["concept_ids"])
        return result, context

    @staticmethod
    def save(session_id: str, plan: dict, user_message="", *, turn_id=None, database=None) -> None:
        """保存目标、学习步骤和待澄清问题，供下一轮或页面恢复时继续。

        goal_update 会清除旧目标绑定；clarify 则保存原任务与缺失信息，
        不把尚未确定的知识点提前提交成新的教学工作区。
        """
        from contextlib import nullcontext
        with (nullcontext(database) if database is not None else db_session()) as db:
            row = db.get(LearningSession, session_id, with_for_update=True)
            if row is None or (turn_id is not None and row.active_turn_id != turn_id):
                raise LearningTurnLost("主Agent不再持有当前会话，不能提交过时计划")
            pending = dict(row.pending_context)
            previous = pending.pop("decision", None)
            changed_goal = bool(
                plan.get("goal_update") and plan["goal_update"] != row.goal
            )
            if changed_goal:
                quote = plan.get("goal_update_request")
                if not quote or quote not in user_message:
                    raise ValueError("更换整体目标必须引用当前用户的明确更换要求")
                if plan.get("session_goal_update"):
                    raise ValueError("更换整体目标时不能同时改写旧目标图")
            # 先建立本会话旧根，避免整体目标变更时丢失此前的目标历史。
            ensure_session_goals(db, row)
            if plan.get("exercise_id"):
                pending["discussion_exercise_id"] = plan["exercise_id"]
            if plan["action"] == "clarify":
                decision = dict(plan.get("clarification") or {})
                decision.setdefault(
                    "objective",
                    (previous or {}).get("objective")
                    or user_message[:1000]
                    or row.goal,
                )
                decision["question"] = plan["message"]
                pending["decision"] = decision
            if plan.get("goal_update"):
                row.goal = plan["goal_update"]
                row.agenda = []
                row.concept_ids = []
            # 澄清阶段保留已有工作区，新任务不受它限制；选中具体目标后再提交。
            if plan["concept_ids"] and plan["action"] != "clarify":
                row.concept_ids = list(plan["concept_ids"])
            if plan["steps"]:
                row.agenda = list(plan["steps"])
            row.pending_context = pending
            save_progress(row, plan.get("continuation"))
            apply_goal_update(
                db,
                row,
                plan.get("session_goal_update"),
                continuation=plan.get("continuation"),
                overall_changed=changed_goal,
            )
            row.version += 1
