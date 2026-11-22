"""普通学生话语与公开讲题回执的客观审计，不推断教学质量。"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import re
import unicodedata

_TEACHING_DIRECTIVES = (
    "启发",
    "苏格拉底",
    "中性邀请",
    "先停顿",
    "自然停顿",
    "不追加",
    "不要追加",
    "不要追问",
    "别追问",
    "不要给答案",
    "别给答案",
    "不直接给",
    "不要直接给",
    "别直接给",
    "给建模结构",
    "字以内",
    "help_level",
    "support_annotations",
)
_WITHHOLD_ANSWER = re.compile(
    r"(?:不要|别|先不|暂不).{0,5}(?:告诉|给|说出|透露|展示).{0,5}(?:答案|解法)"
)


def assert_natural_student_text(text: str) -> None:
    """拒绝指导教师教学策略的输入；不改写输入，也不限制正常学习需求。"""
    assert isinstance(text, str) and text.strip(), "学生输入必须是非空字符串。"
    normalized = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).lower()
    found = [word for word in _TEACHING_DIRECTIVES if word in normalized]
    assert not found, "学生输入含教学元指令：" + ", ".join(found)
    assert not _WITHHOLD_ANSWER.search(
        normalized
    ), "学生输入含要求隐藏答案的教学元指令。"


def audit_published_turn(turn: dict) -> dict:
    """核对发布安全与提交前证据，返回供人工评价教学行为的可序列化事实。"""
    events = turn["sse_events"]
    message = turn["public_message"]
    guidance = turn["guidance"]
    evidence = turn["evidence"]
    feedback = turn["evidence_feedback"]
    support = turn["new_support_events"]
    context = evidence["response_context"]

    assert events and events[-1]["event"] == "done", "SSE没有正常结束。"
    assert not any(item["event"] == "error" for item in events), "SSE存在失败事件。"
    assert not any(
        item["event"] == "text_reset" for item in events
    ), "校验后发布不应先泄漏正文再撤回。"
    assert message["role"] == "assistant", "公开消息不是教师消息。"
    assert (
        message["metadata"]["delivery_mode"] == "validated_publication"
    ), "教师正文没有使用校验后发布。"
    deltas = [item["data"] for item in events if item["event"] == "text_delta"]
    assert deltas, "没有观察到校验后正文发布事件。"
    assert all(
        item.get("delivery_mode") == "validated_publication" for item in deltas
    ), "存在未标记校验后发布的正文增量。"
    published_text = "".join(item["delta"] for item in deltas)
    assert published_text == message["content"], "SSE正文与最终教师消息不一致。"
    assistant_events = [
        item["data"]
        for item in events
        if item["event"] == "message" and item["data"].get("role") == "assistant"
    ]
    assert len(assistant_events) == 1, "一次教学提交应只发布一条正式教师消息。"
    assert assistant_events[0] == message, "SSE正式教师消息与审计消息不一致。"

    # 学生输入类型仍来自模型观察，独立条件只能按冻结快照机械核对。
    expected_attempt = (
        evidence["requested_action"] == "reply"
        and evidence["student_input_kind"] == "attempt"
    )
    expected_assisted = bool(context["assisted"])
    expected_independent = bool(
        expected_attempt
        and context["problem_kind"] == "transfer"
        and not expected_assisted
    )
    assert evidence["is_attempt"] == expected_attempt
    assert evidence["assisted_before_response"] == expected_assisted
    assert evidence["independent_response"] == expected_independent
    for key in (
        "student_input_kind",
        "is_attempt",
        "assisted_before_response",
        "independent_response",
    ):
        assert feedback[key] == evidence[key], f"公开证据说明与后端证据不一致：{key}"
    expected_condition = (
        "not_attempt"
        if not expected_attempt
        else (
            "assisted"
            if expected_assisted
            else (
                "independent_recorded_conditions"
                if expected_independent
                else "no_recorded_help"
            )
        )
    )
    assert feedback["condition"] == expected_condition
    assert message["metadata"]["evidence_feedback"] == feedback
    assert isinstance(feedback["statement"], str)
    if feedback["statement"]:
        assert feedback["statement"] in message["content"]

    # 本轮新发布的提示不能倒灌进本轮作答前的帮助记录。
    before_ids = context["support_event_ids"]
    new_ids = [item["id"] for item in support]
    assert not set(before_ids).intersection(new_ids), "答后帮助倒灌进提交前快照。"
    transfer = guidance.get("transfer") or {}
    return {
        "teaching_action": guidance.get("teaching_action", guidance.get("action")),
        "response_expectation": guidance.get("response_expectation"),
        "pending_question": guidance.get("pending_question", ""),
        "pending_question_id": guidance.get("pending_question_id"),
        "public_focus": guidance.get("focus"),
        "public_analysis_summary": guidance.get("analysis_summary"),
        "public_sources": deepcopy(guidance.get("sources", [])),
        "public_citations": deepcopy(message.get("citations", [])),
        "new_problem_id": transfer.get("id"),
        "new_problem_stem": transfer.get("stem"),
        "new_problem_target": transfer.get("target"),
        "new_problem_assisted": transfer.get("assisted"),
        "new_support_event_count": len(support),
        "new_support_events": deepcopy(support),
        "response_problem_id": context["problem_id"],
        "response_problem_kind": context["problem_kind"],
        "student_input_kind": evidence["student_input_kind"],
        "is_attempt": expected_attempt,
        "assisted_before_response": expected_assisted,
        "independent_response": expected_independent,
        "support_event_ids_before_response": deepcopy(before_ids),
        "evidence_condition": feedback["condition"],
        "evidence_statement": feedback["statement"],
        "input_classification_method": evidence.get("input_classification_method"),
        "delivery_mode": "validated_publication",
        "text_delta_count": len(deltas),
        "published_text_sha256": hashlib.sha256(
            message["content"].encode("utf-8")
        ).hexdigest(),
        "sse_matches_final_message": True,
        "feedback_matches_submission_snapshot": True,
    }


def capture_all_run_usage(report: dict) -> None:
    """按验收会话汇总全部Agent运行，保留旧审计的请求次数与重试指标。"""
    identifiers = list(dict.fromkeys(report.get("sessions", {}).values()))
    if not identifiers:
        return
    if __package__:
        from .check_guided_teaching import capture_runtime_audit
    else:
        from check_guided_teaching import capture_runtime_audit

    from sqlalchemy import select

    from app.infrastructure.persistence.database import db_session
    from app.infrastructure.persistence.execution_models import AgentRun

    # 旧函数已统计所有会话的模型请求；仅其Agent运行查询限制了讲题skill。
    capture_runtime_audit(report)
    with db_session() as db:
        runs = list(
            db.scalars(
                select(AgentRun)
                .where(AgentRun.session_id.in_(identifiers))
                .order_by(AgentRun.created_at, AgentRun.id)
            )
        )
        report["database_dialect"] = db.get_bind().dialect.name
        report["actual_models"] = sorted({run.model for run in runs})
        report["run_outcomes"] = [
            {
                "id": run.id,
                "session_id": run.session_id,
                "agent": run.agent,
                "skill": run.skill,
                "status": run.status,
                "error": run.error,
            }
            for run in runs
        ]
        report["model_usage"].update(
            run_count=len(runs),
            failed_run_count=sum(run.status != "completed" for run in runs),
            prompt_tokens=sum(run.prompt_tokens or 0 for run in runs),
            completion_tokens=sum(run.completion_tokens or 0 for run in runs),
            model_duration_ms=round(sum(run.duration_ms or 0 for run in runs), 2),
        )
