"""验证模型的帮助身份归因被拦截，统一说明只使用提交前证据条件。"""

from copy import deepcopy

import pytest

from app.domains.guidance.feedback import finalize_feedback, model_feedback_claims, response_record, validate_model_feedback
from app.domains.guidance.schemas import GuidanceRequest


@pytest.mark.parametrize(
    "claim",
    [
        "你这次是在没有任何提示的情况下独立完成的。",
        "你这次是在只得到一条局部提示的情况下完成的。",
        "你没有独立完成这道题。",
        "这次属于辅助作答。",
        "你是在老师帮助下完成的。",
        "你的求解过程依赖我给的提示。",
        "全靠你自己想出来的，过程很完整。",
        "上次在提示下完成，这次的过程正确。",
        "独立完成本题，推理正确。",
        "你已经自主解答了这道题。",
        "如果我没看错，你是在提示下完成的。",
        "你的作答没有依靠任何点拨。",
        "You solved this independently.",
        "You solved this with my help.",
        "“你已经独立完成了”，这句话是正确的。",
        "你说“我独立完成了”，这确实说明你的答案是独立完成的。",
    ],
)
def test_rejects_both_independent_and_assisted_attributions(claim):
    with pytest.raises(ValueError, match="提交前帮助快照"):
        validate_model_feedback({"content": claim})


@pytest.mark.parametrize(
    "field",
    [
        {"content": "你独立完成了。"},
        {"question": "你独立完成了，为什么这样列式？"},
        {"focus": "学生独立完成了求解"},
        {"observation": "学生在提示下完成了本题。"},
        {"analysis": {"summary": "你已经独立解答了。"}},
        {"analysis": {"reference_solution": "学生只得到一个提示后完成了本题。"}},
        {"analysis": {"solution_paths": [{"outline": "你自主完成了求解。"}]}},
        {"transfer_problem": {"target": "学生独立完成了本次验证"}},
    ],
)
def test_persistent_observation_and_nested_fields_are_checked(field):
    assert model_feedback_claims(field)


@pytest.mark.parametrize(
    "content",
    [
        "请独立作答，先写出你的想法。",
        "接下来请独立完成这道新题。",
        "独立作答不等于掌握。",
        "独立完成一道题不代表已经掌握。",
        "若你独立完成这道题，只能说明本次表现。",
        "如果学生在提示下完成，应记为辅助作答。",
        "你说：“我独立完成了。”我会先检查具体的推理过程。",
        "学生原话是“我独立完成了”，这里先核对方程是否正确。",
        "不能说“你已经独立完成了”；应先核对帮助记录。",
        "独立事件满足乘法公式，本题需要检查两个事件是否独立。",
        "你已经证明这两个向量线性独立了。",
        "先解出两个独立方程中的未知量。",
        "你将图书费写为12x、再加固定快递费，数量关系正确。",
        "提示：先看看每本图书的费用和一次性快递费。",
    ],
)
def test_task_instructions_math_terms_quotes_and_conditions_are_not_claims(content):
    validate_model_feedback({"content": content})


def test_condition_cannot_hide_actual_turn_claim():
    with pytest.raises(ValueError):
        validate_model_feedback(
            {"content": "若你独立完成可以检查，但你这次已经独立完成了。"}
        )


def context(*, assisted=False, kind="transfer"):
    return {
        "assisted": assisted,
        "problem_kind": kind,
        "help_level": 2 if assisted else 0,
        "support_event_ids": ["earlier-support"] if assisted else [],
    }


def attempt_request():
    return GuidanceRequest(
        action="reply", text="我先减去快递费，再除以单价。", request_id="attempt-01"
    )


def test_second_assisted_attempt_cannot_repeat_first_independent_claim():
    request = attempt_request()
    output = {
        "content": "数量关系成立。",
        "student_input_kind": "attempt",
        "observation": "计算步骤完整。",
    }
    _, first = finalize_feedback(output, request, context())
    assert first["independent_response"] is True
    assert first["condition"] == "independent_recorded_conditions"
    assert "不证明没有外部帮助" in first["statement"]
    with pytest.raises(ValueError):
        finalize_feedback(
            {**output, "content": "你这次是在没有任何提示的情况下独立完成的。"},
            request,
            context(assisted=True),
        )
    clean, second = finalize_feedback(output, request, context(assisted=True))
    assert second["condition"] == "assisted"
    assert second["assisted_before_response"] is True
    assert second["independent_response"] is False
    assert "辅助条件" in second["statement"]
    assert clean == output


def test_no_recorded_help_is_not_overridden_by_model_assistance_claim():
    output = {"content": "你在我的提示下完成了。", "student_input_kind": "attempt"}
    with pytest.raises(ValueError):
        finalize_feedback(output, attempt_request(), context())


@pytest.mark.parametrize("kind", ["question", "acknowledgement", "other"])
def test_non_attempt_does_not_get_independence_statement(kind):
    record = response_record(attempt_request(), {"student_input_kind": kind}, context())
    assert record["condition"] == "not_attempt"
    assert record["statement"] == ""
    assert record["is_attempt"] is False
    assert record["independent_response"] is False


def test_original_problem_without_recorded_help_is_not_transfer_evidence():
    record = response_record(
        attempt_request(), {"student_input_kind": "attempt"}, context(kind="original")
    )
    assert record["condition"] == "no_recorded_help"
    assert record["independent_response"] is False


def test_explain_action_never_creates_attempt_by_model_classification_alone():
    request = GuidanceRequest(action="explain", request_id="explain-01")
    record = response_record(request, {"student_input_kind": "attempt"}, context())
    assert record["is_attempt"] is False


def test_finalize_does_not_mix_server_statement_into_teaching_or_mutate_output():
    output = {
        "content": "数量关系正确，可以继续核对计算。",
        "observation": "列式和代回检验完整。",
        "student_input_kind": "attempt",
    }
    original = deepcopy(output)
    clean, record = finalize_feedback(output, attempt_request(), context(assisted=True))
    assert output == original
    assert clean == original
    assert clean is not output
    assert record["statement"]
    assert "作答记录" not in clean["content"]
    assert "作答记录" not in clean["observation"]
