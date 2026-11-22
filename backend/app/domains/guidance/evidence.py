"""按学生提交前的帮助快照记录作答，教师反馈只影响后续作答。"""

from copy import deepcopy
from uuid import uuid4


def capture_response_context(state):
    """只读当前工作区，在模型生成本轮反馈前冻结学生已看过的帮助。"""
    transfer = state.get("transfer")
    target = transfer or state["problem"]
    support = [
        event
        for event in state.get("support_history", [])
        if event["problem_id"] == target["id"]
    ]
    return {
        "workspace_id": state["id"],
        "workspace_version": state["version"],
        "policy_version": "3",
        "problem_id": target["id"],
        "problem_kind": "transfer" if transfer else "original",
        "question_id": state["pending_question_id"],
        "question": state.get("pending_question", ""),
        "response_expectation": state["response_expectation"],
        "assisted": bool(
            transfer.get("assisted", False)
            if transfer
            else state.get("assisted", False)
        ),
        "help_level": max(
            [event["help_level"] for event in support]
            + [target.get("help_level", 0) if transfer else state.get("help_level", 0)]
        ),
        "support_event_ids": [event["id"] for event in support],
        "assistance_basis": (
            transfer.get("assistance_basis", "recorded_support")
            if transfer
            else "recorded_support"
        ),
        "capture_method": "submission_snapshot",
    }


def response_evidence(request, output, context, run_id):
    """独立性描述提交时的帮助条件；输入分类与理解判断仍是模型观察。"""
    kind = output.get("student_input_kind", "other")
    is_attempt = request.action == "reply" and kind == "attempt"
    transfer_response = (
        request.action == "reply" and context["problem_kind"] == "transfer"
    )
    return {
        "id": f"{context['workspace_id']}:{request.request_id}",
        "request_id": request.request_id,
        "raw_input": request.text,
        "requested_action": request.action,
        "teaching_action": output["action"],
        "pending_question_before": context["question"],
        "response_context": deepcopy(context),
        "student_input_kind": kind,
        "input_classification_method": "model_observation",
        "is_attempt": is_attempt,
        "assisted_before_response": context["assisted"],
        "model_observation": output["observation"],
        "model_understanding": output["understanding"],
        "assessment_method": "model_observation_not_program_proof",
        "run_id": run_id,
        "source_ids": output["source_ids"],
        "transfer_response": transfer_response,
        "independent_response": bool(
            is_attempt and transfer_response and not context["assisted"]
        ),
    }


def record_support(state, request, output, target, *, help_level=None, exposure=None):
    """保存已发布帮助的内容和归属；原题示范与附带新题分别记账。"""
    level = (
        exposure["help_level"]
        if exposure is not None
        else (
            max(
                output["help_level"],
                {"hint": 1, "explain": 1, "worked_example": 3}.get(output["action"], 0),
            )
            if help_level is None
            else help_level
        )
    )
    if level <= 0:
        return False
    state.setdefault("support_history", []).append(
        {
            "id": str(uuid4()),
            "problem_id": target["id"],
            "request_id": request.request_id,
            "workspace_version": state["version"],
            "teaching_action": output["action"],
            "help_level": level,
            "content": exposure["content"] if exposure else output["content"],
            "question": exposure["question"] if exposure else output["question"],
            "exposure_types": exposure.get("exposure_types", []) if exposure else [],
            "detection_method": (
                exposure["detection_method"] if exposure else "legacy_help_level"
            ),
        }
    )
    return True
