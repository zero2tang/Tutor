"""依据真实原答独立诊断；只有此服务提交有效证据。"""

from uuid import uuid4
from sqlalchemy import select

from app.infrastructure.persistence.database import db_session
from app.domains.assessment.models import Attempt
from app.domains.sessions.models import LearningSession
from app.domains.students.models import MemoryItem
from app.agent.runtime.contracts import AgentRunner, EventSink
from app.domains.assessment.schemas import Diagnosis
from app.domains.assessment.grading import deterministic_grade
from app.domains.sessions.progress import preserve_learning_context
from app.domains.courses.catalog import CatalogService
from app.domains.students.service import StudentService
from app.domains.assessment.rules import finalize_diagnosis
from app.domains.assessment.evidence import (
    freeze_grading_packet,
    persist_concept_links,
    load_mastery_evidence,
    fingerprint,
    submission_help_snapshot,
)
from app.agent.tools.contracts import ToolContext

# 总类标签不作为可独立验证的能力；数学节点不受力学总标签策略影响。
ASSESSMENT_EXCLUDED_CONCEPTS = {"mechanics": {"mechanics:c198"}}


class AssessmentService:
    def __init__(self, runner: AgentRunner):
        """连接评估角色、课程事实和学生证据服务，统一控制判分与状态提交。"""
        self.runner = runner
        self.student = StudentService()
        self.catalog = CatalogService()

    def existing(self, session_id: str, key: str, answer: str) -> dict | None:
        """按会话和提交标识查找既有评估；相同标识携带不同答案时拒绝复用。"""
        with db_session() as db:
            row = db.scalar(
                select(Attempt).where(
                    Attempt.session_id == session_id, Attempt.idempotency_key == key
                )
            )
            if row:
                if row.answer_text != answer:
                    raise ValueError("同一提交标识不能对应不同答案")
                return self.serialize(row, replay=True)
        return None

    @staticmethod
    def serialize(row: Attempt, *, replay: bool = False) -> dict:
        """组装已提交作答的反馈，包括诊断、实际状态变化和是否为幂等回放。"""
        return {
            "attempt_id": row.id,
            "question_id": row.question_id,
            "result": row.result,
            "score": row.score,
            "assisted": row.assisted,
            "diagnosis": row.assessment,
            "state_updates": row.state_updates,
            "replayed": replay,
        }

    def commit_attempt(
        self, db, session, contract, answer, diagnosis, target_ids, *, assisted, key
    ):
        """只借用调用方事务，不提交；原答、逐概念证据与能力更新共同成功。"""
        row = Attempt(
            id=str(uuid4()),
            session_id=session.id,
            question_id=contract["id"],
            answer_text=answer,
            result=diagnosis["result"],
            score=diagnosis["score"],
            assisted=assisted,
            assessment=diagnosis,
            state_updates=[],
            idempotency_key=key,
        )
        persist_concept_links(db, row, session, diagnosis["grading_packet"])
        row.state_updates = self.student.apply_evidence(
            db,
            concept_ids=target_ids,
            attempt_id=row.id,
            question_id=contract["id"],
            diagnosis=diagnosis,
            assisted=row.assisted,
            student_id=session.student_id,
            evidence_ledger=load_mastery_evidence(
                db,
                packet=diagnosis["grading_packet"],
                student_id=session.student_id,
                concept_ids=target_ids,
                attempt_id=row.id,
            ),
        )
        # apply_evidence 使用同一个事务；作答保存成功不代表一定更新掌握度。
        db.add(row)
        if (
            len(contract["concept_ids"]) == 1
            and diagnosis.get("misconception")
            and row.result
            in {
                "incorrect",
                "partial",
            }
        ):
            db.add(
                MemoryItem(
                    student_id=session.student_id,
                    course_id=session.course_id,
                    concept_id=target_ids[0] if target_ids else None,
                    kind="misconception",
                    text=diagnosis["misconception"],
                    evidence_ids=[row.id],
                )
            )
        return row

    async def assess(self, session_id, answer, key, turn_id, emit):
        prepared = await self.prepare(session_id,answer,key,turn_id,emit)
        return await self.commit(session_id,answer,key,turn_id,emit,prepared)

    async def prepare(
        self, session_id: str, answer: str, key: str, turn_id: str, emit: EventSink
    ) -> dict:
        """以冻结题目为依据完成判分、模型诊断和证据的原子提交。

        规则能判定时以规则结果为准，模型补充错因、层级和反思。
        模型请求结束后再加行锁，在同一事务保存 Attempt、受约束的掌握度
        变化及错因记忆；无法判定时保留待答状态，允许用户补充说明。
        """
        existing = self.existing(session_id, key, answer)
        if existing:
            return {"existing": existing}
        with db_session() as db:
            session = db.get(LearningSession, session_id)
            if not session or not session.pending_question_id:
                raise ValueError("当前没有等待作答的题目")
            pending = dict(session.pending_context)
            if pending.get("exercise_batch"):
                raise ValueError("当前是一组练习，请一次提交整组答案")
            contract = pending["contract"]
            expected_question = session.pending_question_id
            student_id = session.student_id
            course_id = session.course_id
        self.catalog.course(course_id)
        if contract.get("course_id") != course_id:
            raise ValueError("冻结题目与会话课程不一致")
        if contract.get("id") != expected_question:
            raise ValueError("冻结题目与当前待答题不一致")
        for cid in contract["concept_ids"]:
            self.catalog.concept(cid, course_id)
        excluded = ASSESSMENT_EXCLUDED_CONCEPTS.get(course_id, set())
        target_ids = [
            cid
            for cid in pending.get("target_concept_ids", [])
            if cid in contract["concept_ids"] and cid not in excluded
        ]
        target_ids = (
            target_ids
            or [cid for cid in contract["concept_ids"] if cid not in excluded][:3]
        )
        context = ToolContext(
            course_id=course_id,
            session_id=session_id,
            concept_ids=target_ids,
            excluded_question_ids=[expected_question],
            student_id=student_id,
            # 评分和主模型接续共用原请求，失败后不能各自重置运行额度。
            request_id=key,
        )
        objective = deterministic_grade(contract, answer)
        level = {
            "recall": "beginner",
            "understanding": "advanced",
            "application": "proficient",
        }[pending.get("cognitive_type", "understanding")]
        fixed_level = level if contract.get("cognitive_type") != "unknown" else None
        # 原题没有认知标签时，用户选择的练习层级只是上限，不能当作题目标注。
        result = await self.runner.run(
            "assessment",
            "diagnose",
            {
                "question": contract,
                "student_answer": answer,
                "deterministic_verdict": objective,
                "target_level": fixed_level,
                "requested_level": level,
                "assisted": bool(pending.get("assisted")),
                "help_snapshot": submission_help_snapshot(pending),
                "instruction": "本次是独立评分。仅依据冻结题目、学生原答和提交前帮助条件判分，不读取或推测历史学生状态与作答前预测。综合题逐概念只能使用assessment_criteria中已有要点和原答准确片段，没有局部依据就not_observed。原题认知类型unknown时，requested_level只是上限。",
            },
            Diagnosis,
            context,
            turn_id,
            emit=emit,
            context_purpose="grading",
            use_tools=False,
        )
        diagnosis = finalize_diagnosis(
            result["output"],
            contract,
            pending.get("cognitive_type", "understanding"),
            objective,
            result["run_id"],
        )
        packet = freeze_grading_packet(contract, answer, diagnosis, target_ids,
            bool(pending.get("assisted")), submission_help_snapshot(pending), result["run_id"])
        diagnosis["grading_packet"] = packet
        return {"contract":contract,"pending":pending,"expected_question":expected_question,"target_ids":target_ids,"diagnosis":diagnosis}

    async def commit(self, session_id, answer, key, turn_id, emit, prepared, *, publish=True):
        """原答与能力证据只在此事务提交；图重试先检查正式回执。"""
        if "existing" in prepared:
            return prepared["existing"]
        contract,pending,expected_question,target_ids,diagnosis = (prepared[k] for k in ("contract","pending","expected_question","target_ids","diagnosis"))
        with db_session() as db:
            session = db.get(LearningSession, session_id, with_for_update=True)
            # 图节点的预检查不能替代事务内校验；清空 owner 同样表示撤销执行权。
            if not turn_id or session is None or session.active_turn_id != turn_id:
                from app.shared.execution import LearningTurnLost
                raise LearningTurnLost("评分提交已失去执行权")
            # 推理期间可能发生重试或题目变化，写入前必须再次确认幂等与待答题号。
            repeated = db.scalar(
                select(Attempt).where(
                    Attempt.session_id == session_id, Attempt.idempotency_key == key
                )
            )
            if repeated:
                if repeated.answer_text != answer:
                    raise ValueError("同一提交标识不能对应不同答案")
                return self.serialize(repeated, replay=True)
            from app.domains.memory.context import invalidation_epoch
            if prepared.get("memory_epoch") is not None and invalidation_epoch(db, session.student_id, lock=True) != prepared["memory_epoch"]:
                raise ValueError("评分使用的记忆已撤销，拒绝提交旧候选。")
            if session.pending_question_id != expected_question:
                raise ValueError("题目状态已经变化，请刷新后重试")
            if fingerprint(session.pending_context.get("contract")) != fingerprint(contract) or submission_help_snapshot(session.pending_context) != submission_help_snapshot(pending):
                raise ValueError("评分期间题目或帮助条件发生变化，请刷新后重试")
            row = self.commit_attempt(
                db,
                session,
                contract,
                answer,
                diagnosis,
                target_ids,
                assisted=bool(pending.get("assisted")),
                key=key,
            )
            if row.result != "ungraded":
                session.pending_question_id = None
                session.pending_context = {
                    **preserve_learning_context(session.pending_context),
                    "last_question_id": expected_question,
                    "last_contract": contract,
                }
                session.phase = "learning"
            else:
                session.phase = "awaiting_answer"
            session.version += 1
            db.flush()
            response = self.serialize(row)
        if publish:
            await emit("assessment_completed", response)
        return response
