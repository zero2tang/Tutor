"""一次评估 Agent 运行分析整组答案，逐题证据与幂等结果原子提交。"""

from copy import deepcopy
from hashlib import sha256
import json
from uuid import uuid4

from sqlalchemy import select

from app.infrastructure.persistence.database import db_session
from app.domains.assessment.models import Attempt, BatchSubmission
from app.domains.sessions.models import LearningSession, Message
from app.domains.students.models import MemoryItem
from app.domains.assessment.schemas import BatchAnswerRequest, BatchDiagnosis
from app.domains.assessment.service import ASSESSMENT_EXCLUDED_CONCEPTS, AssessmentService
from app.domains.assessment.rules import COGNITIVE_LEVEL, finalize_diagnosis
from app.domains.assessment.grading import deterministic_grade
from app.domains.assessment.evidence import (
    freeze_grading_packet,
    persist_concept_links,
    load_mastery_evidence,
    submission_help_snapshot,
)
from app.domains.sessions.progress import preserve_learning_context
from app.domains.sessions.service import message_dict
from app.agent.tools.contracts import ToolContext


def submission_hash(request: BatchAnswerRequest) -> str:
    value = {
        "batch_id": request.batch_id,
        "answers": sorted(
            [(item.question_id, item.answer) for item in request.answers]
        ),
    }
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


class BatchAssessmentService:
    def __init__(self, assessment: AssessmentService):
        self.assessment = assessment

    def existing(self, session_id, request):
        with db_session() as db:
            row = db.scalar(
                select(BatchSubmission).where(
                    BatchSubmission.session_id == session_id,
                    BatchSubmission.idempotency_key == request.request_id,
                )
            )
            if row:
                if row.payload_hash != submission_hash(request):
                    raise ValueError("同一提交标识不能对应不同练习组或答案")
                return {**deepcopy(row.response), "replayed": True}
        return None

    async def assess(self, session_id, request, turn_id, emit):
        prepared=await self.prepare(session_id,request,turn_id,emit)
        return await self.commit(session_id,request,turn_id,emit,prepared)

    async def prepare(self, session_id, request, turn_id, emit):
        replay = self.existing(session_id, request)
        if replay:
            return {"existing": replay}
        with db_session() as db:
            session = db.get(LearningSession, session_id)
            batch = (
                deepcopy((session.pending_context or {}).get("exercise_batch"))
                if session
                else None
            )
            if not batch or batch["id"] != request.batch_id:
                raise ValueError("练习组已变化或不属于当前会话，请刷新后重试")
            if session.active_turn_id != turn_id:
                raise ValueError("评估任务未持有当前会话")
            course_id, student_id = session.course_id, session.student_id
        answers = {item.question_id: item.answer for item in request.answers}
        expected = {item["contract"]["id"] for item in batch["items"]}
        if len(answers) != len(request.answers) or set(answers) != expected:
            raise ValueError(
                "必须为练习组中的每一道题提交一次答案，不能重复、遗漏或添加其他题目"
            )
        if any(not answer.strip() for answer in answers.values()):
            raise ValueError("请完成每一道题后再提交")
        assessment = self.assessment
        assessment.catalog.course(course_id)
        packets, targets, objectives = [], {}, {}
        excluded = ASSESSMENT_EXCLUDED_CONCEPTS.get(course_id, set())
        for item in batch["items"]:
            contract = item["contract"]
            qid = contract["id"]
            if contract.get("course_id") != course_id:
                raise ValueError("冻结练习组包含其他课程的题目")
            for cid in contract["concept_ids"]:
                assessment.catalog.concept(cid, course_id)
            targets[qid] = [
                cid
                for cid in item["target_concept_ids"]
                if cid in contract["concept_ids"] and cid not in excluded
            ]
            objectives[qid] = deterministic_grade(contract, answers[qid])
            level = COGNITIVE_LEVEL[item["cognitive_type"]]
            packets.append(
                {
                    "question_id": qid,
                    "question": contract,
                    "student_answer": answers[qid],
                    "deterministic_verdict": objectives[qid],
                    "assisted": item["assisted"],
                    "help_snapshot": submission_help_snapshot(item),
                    "requested_level": level,
                    "target_level": (
                        level if contract.get("cognitive_type") != "unknown" else None
                    ),
                }
            )
        concepts = list(dict.fromkeys(cid for ids in targets.values() for cid in ids))
        context = ToolContext(
            course_id=course_id,
            student_id=student_id,
            request_id=request.request_id,
            session_id=session_id,
            concept_ids=concepts,
            excluded_question_ids=sorted(expected),
        )

        def validate(output):
            ids = [item["question_id"] for item in output["items"]]
            if len(ids) != len(expected) or set(ids) != expected:
                raise ValueError("逐题诊断必须完整覆盖本组题号且不能重复，不得新增题号")

        result = await assessment.runner.run(
            "assessment",
            "diagnose",
            {
                "task": "batch_diagnose",
                "questions": packets,
                "instruction": "独立评分整组真实原答，不读取历史学生标签或预测。逐题简明反馈，遵守每题deterministic_verdict。综合题concept_results只能引用该题assessment_criteria和学生原答准确片段，没有局部依据保持not_observed。summary只能总结本次观察，不得宣称已经掌握整个课程。",
            },
            BatchDiagnosis,
            context,
            turn_id,
            emit=emit,
            use_tools=False,
            validate_output=validate,
            max_tokens=8000,
            context_purpose="grading",
        )
        validate(result["output"])
        proposed = {
            item["question_id"]: item["diagnosis"] for item in result["output"]["items"]
        }
        return {"batch":batch,"answers":answers,"targets":targets,"objectives":objectives,"result":result,"proposed":proposed}

    async def commit(self, session_id, request, turn_id, emit, prepared, *, publish=True):
        """整组一次事务提交；模型诊断准备与正式写入分属图节点。"""
        if "existing" in prepared:
            return prepared["existing"]
        batch,answers,targets,objectives,result,proposed = (prepared[k] for k in ("batch","answers","targets","objectives","result","proposed"))
        assessment=self.assessment
        with db_session() as db:
            session = db.get(LearningSession, session_id, with_for_update=True)
            repeated = db.scalar(
                select(BatchSubmission).where(
                    BatchSubmission.session_id == session_id,
                    BatchSubmission.idempotency_key == request.request_id,
                )
            )
            if repeated:
                if repeated.payload_hash != submission_hash(request):
                    raise ValueError("同一提交标识不能对应不同答案")
                return {**deepcopy(repeated.response), "replayed": True}
            if (
                not turn_id or session is None or session.active_turn_id != turn_id
                or session.pending_context.get("exercise_batch") != batch
            ):
                raise ValueError("练习组状态已变化，不能提交旧评估")
            from app.domains.memory.context import invalidation_epoch
            if prepared.get("memory_epoch") is not None and invalidation_epoch(db, session.student_id, lock=True) != prepared["memory_epoch"]:
                raise ValueError("整组评分使用的记忆已撤销，拒绝提交旧候选。")
            items = []
            for item in batch["items"]:
                contract = item["contract"]
                qid = contract["id"]
                diagnosis = finalize_diagnosis(
                    proposed[qid],
                    contract,
                    item["cognitive_type"],
                    objectives[qid],
                    result["run_id"],
                )
                diagnosis["batch_id"] = batch["id"]
                packet = freeze_grading_packet(contract, answers[qid], diagnosis, targets[qid],
                    item["assisted"], submission_help_snapshot(item), result["run_id"])
                diagnosis["grading_packet"] = packet
                attempt = assessment.commit_attempt(
                    db,
                    session,
                    contract,
                    answers[qid],
                    diagnosis,
                    targets[qid],
                    assisted=item["assisted"],
                    key="batch:"
                    + sha256(
                        (request.request_id + ":" + qid).encode()
                    ).hexdigest(),
                )
                # 同组同知识点的下一份证据必须看到本次初始化，避免重复创建状态行。
                db.flush()
                items.append(assessment.serialize(attempt))
            scores = [item["score"] for item in items if item["score"] is not None]
            response = {
                "batch_id": batch["id"],
                "items": items,
                "summary": result["output"]["summary"],
                "next_action": result["output"]["next_action"],
                "run_id": result["run_id"],
                "correct_count": sum(item["result"] == "correct" for item in items),
                "graded_count": len(scores),
                "total": len(items),
                "score": round(sum(scores) / len(scores), 3) if scores else None,
                "replayed": False,
            }
            messages = []
            if publish:
                user = Message(
                    session_id=session_id,
                    role="user",
                    agent="",
                    content="提交整组练习答案。",
                    metadata_json={
                        "kind": "batch_answer",
                        "batch_id": batch["id"],
                        "answers": answers,
                    },
                )
                feedback = Message(
                    session_id=session_id,
                    role="assistant",
                    agent="assessment",
                    content=response["summary"],
                    metadata_json={
                        "kind": "batch_assessment",
                        "batch_assessment": response,
                    },
                )
                db.add_all([user, feedback])
                db.flush()
                messages = [message_dict(user), message_dict(feedback)]
            db.add(BatchSubmission(session_id=session_id, batch_id=batch["id"],
                idempotency_key=request.request_id, payload_hash=submission_hash(request), response=response))
            session.pending_question_id = None
            session.pending_context = {
                **preserve_learning_context(session.pending_context),
                "last_exercise_batch": batch,
            }
            session.phase = "learning"
            session.version += 1
            db.flush()
        if publish:
            for message in messages:
                await emit("message", message)
            await emit("batch_assessment_completed", response)
        return response
