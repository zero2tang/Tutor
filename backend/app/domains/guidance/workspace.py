"""通用教学工作区及公开投影，原始作答与模型判断明确分开。"""

from copy import deepcopy
from hashlib import sha256
import json
import re
import unicodedata
from uuid import uuid4

from app.domains.guidance.evidence import (
    capture_response_context,
    record_support,
    response_evidence,
)
from app.domains.guidance.exposure import prepare_publication

SKILL_NAME = "guided_explanation"
SKILL_VERSION = "3"


def fingerprint(value) -> str:
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def problem_hash(stem: str) -> str:
    # 只用于文本去重，不伪称能识别所有数学等价题或学生在站外见过的题。
    return sha256(
        re.sub(r"\s+", "", unicodedata.normalize("NFKC", stem)).encode()
    ).hexdigest()


def initial_workspace(problem: dict, course_id: str) -> dict:
    return {
        "id": str(uuid4()),
        "course_id": course_id,
        "version": 0,
        "skill": SKILL_NAME,
        "skill_version": SKILL_VERSION,
        "problem": {"id": str(uuid4()), **problem},
        "status": "awaiting_student",
        "analysis": None,
        "analysis_summary": "",
        "pending_question": "",
        "pending_question_id": None,
        "response_expectation": "none",
        "focus": "",
        "help_level": 0,
        "assisted": False,
        "turn_count": 0,
        "sources": [],
        "evidence": [],
        "support_history": [],
        "dialogue": [],
        "transfer": None,
        "seen_problem_hashes": [problem_hash(problem["stem"])],
    }


def public_workspace(state: dict | None) -> dict | None:
    if not state:
        return None
    fields = (
        "id",
        "version",
        "status",
        "problem",
        "pending_question",
        "focus",
        "help_level",
        "assisted",
        "turn_count",
        "sources",
        "analysis_summary",
        "skill",
        "skill_version",
        "closed",
    )
    result = {key: deepcopy(state.get(key)) for key in fields}
    result["response_expectation"] = state["response_expectation"]
    result["pending_question_id"] = state["pending_question_id"]
    transfer = state.get("transfer")
    result["transfer"] = (
        {
            key: deepcopy(transfer.get(key))
            for key in ("id", "stem", "target", "assisted", "response_count")
        }
        if transfer
        else None
    )
    if transfer and not transfer.get("assisted", False):
        # 零帮助验证的限制覆盖所有公开界面，侧栏、来源标题与目标标签也可能泄露方法。
        result["focus"] = ""
        result["analysis_summary"] = ""
        result["sources"] = []
        result["transfer"]["target"] = "新题作答"
    result["evidence_notice"] = (
        "模型教学观察与真实作答分别记录；尚未校准掌握概率，也未验证长期学习效果。"
    )
    return result


def apply_output(state, output, request, sources, run_id, *, response_context=None):
    if "_publication_support" not in output:
        output = prepare_publication(output)
    # 快照只来自生成反馈前的工作区；先记录学生表现，再登记教师提供的新帮助。
    context = deepcopy(response_context or capture_response_context(state))
    evidence = response_evidence(request, output, context, run_id)
    state = deepcopy(state)
    state["skill_version"] = SKILL_VERSION
    if output.get("analysis") is not None:
        state["analysis"] = output["analysis"]
        state["analysis_summary"] = output["analysis"]["summary"]
    state["sources"] = list(
        {item["id"]: item for item in [*state["sources"], *sources]}.values()
    )[-40:]
    state["version"] += 1
    state["turn_count"] += 1
    state["focus"] = output["focus"]
    expectation = (
        "none"
        if output["action"] == "complete"
        else output["response_expectation"]
    )
    state["response_expectation"] = expectation
    state["pending_question"] = output["question"] if expectation != "none" else ""
    state["pending_question_id"] = str(uuid4()) if state["pending_question"] else None
    state["help_level"] = output["help_level"]
    if output["action"] == "complete":
        state["status"] = "completed"
    elif expectation != "answer":
        state["status"] = "ready"
    elif output["action"] == "clarify":
        state["status"] = "awaiting_clarification"
    else:
        state["status"] = "awaiting_student"
    transfer_before = state.get("transfer")
    new_problem = output.get("transfer_problem")
    # 帮助回执与实际发布片段同源；模型自报0不再决定是否得到帮助。
    target = state["problem"] if new_problem else transfer_before or state["problem"]
    helped = False
    for exposure in output["_publication_support"]:
        if exposure["target"] == "current":
            helped = (
                record_support(state, request, output, target, exposure=exposure)
                or helped
            )
    state["assisted"] = state["assisted"] or helped
    if transfer_before:
        if target["id"] == transfer_before["id"]:
            transfer_before["assisted"] = transfer_before["assisted"] or helped
        if evidence["is_attempt"]:
            transfer_before["response_count"] += 1
    if new_problem:
        new_exposures = [
            item for item in output["_publication_support"] if item["target"] == "new"
        ]
        new_help = max(
            [
                new_problem.get("help_level", 0),
                *[item["help_level"] for item in new_exposures],
            ]
        )
        state["transfer"] = {
            "id": str(uuid4()),
            **new_problem,
            "help_level": new_help,
            "assisted": new_help > 0,
            "response_count": 0,
            "origin": "model_generated",
            "content_hash": problem_hash(new_problem["stem"]),
        }
        for exposure in new_exposures:
            record_support(state, request, output, state["transfer"], exposure=exposure)
        if new_help > 0:
            state["assisted"] = True
        state["seen_problem_hashes"].append(problem_hash(new_problem["stem"]))
    state["evidence"].append(evidence)
    # 保留完整原话至消息表；模型窗口有界，同时保留独立的分析与待答问题。
    state["dialogue"] = [
        *state["dialogue"],
        {
            "student_message": request.text,
            "requested_action": request.action,
            "content": output["content"],
            "question": output["question"],
            "question_id": state["pending_question_id"],
            "response_expectation": expectation,
            "observation": output["observation"],
        },
    ][-12:]
    return state
