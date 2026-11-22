"""检查真实漏标文本和受控发布边界，不能用模型零级声明证明无帮助。"""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.domains.guidance.exposure import INDEPENDENT_INTRO, INDEPENDENT_INVITATION, MIXED_INVITATION, prepare_publication, publication_text, validate_transfer_statement
from app.domains.guidance.schemas import GuidedTeachingOutput


def teaching(**changes):
    value = {
        "action": "ask",
        "content": "请描述你的思路。",
        "question": "你目前进行到哪一步？",
        "help_level": 0,
    }
    value.update(changes)
    return GuidedTeachingOutput.model_validate(value).model_dump()


def new_problem(**changes):
    value = {
        "stem": "每本书12元，快递费45元，共花费525元，求购买的本数。",
        "reference_solution": "PRIVATE_REFERENCE_DO_NOT_PUBLISH",
        "target": "列方程解决实际问题",
    }
    value.update(changes)
    return value


def test_actual_missed_structure_is_assistance_even_when_model_claims_zero():
    leaked = (
        "都有一笔固定的费用，再加一笔随人数（或数量）变化的费用，两者合起来等于总花费。"
    )
    result = prepare_publication(teaching(content=leaked))

    assert result["help_level"] >= 2
    support = result["_publication_support"][0]
    assert support["target"] == "current"
    assert support["content"] == leaked
    assert "model_structure" in support["exposure_types"]
    assert support["detection_method"] == "server_publication_boundary"


def test_unrecognized_subject_wording_remains_assistance():
    result = prepare_publication(
        teaching(content="把两个过程拆开考虑，并关注第二阶段。")
    )

    assert result["help_level"] >= 1
    assert result["_publication_support"][0]["exposure_types"] == [
        "unclassified_teaching"
    ]


def test_independent_publication_removes_free_structure_and_preserves_private_audit():
    leaked = "固定费用加随人数变化的费用等于总花费。"
    original = teaching(
        action="transfer",
        content=leaked,
        question="先用总费用减去固定费用，再列方程。",
        transfer_problem=new_problem(teaching_content="先减掉45元。"),
    )
    before = deepcopy(original)
    result = prepare_publication(original)

    assert original == before
    assert result["content"] == INDEPENDENT_INTRO
    assert result["question"] == INDEPENDENT_INVITATION
    assert result["help_level"] == 0
    assert result["transfer_problem"]["help_level"] == 0
    assert result["_publication_support"] == []
    text = publication_text(result)
    assert leaked not in text
    assert "先减掉45元" not in text
    assert "PRIVATE_REFERENCE_DO_NOT_PUBLISH" not in text
    assert (
        result["_publication_audit"]["suppressed_independent_fields"]["content"]
        == leaked
    )


def test_assisted_new_problem_keeps_disclosed_structure_and_forces_help():
    leaked = "固定费用加随本数变化的费用等于总花费。"
    result = prepare_publication(
        teaching(
            action="transfer",
            content=leaked,
            transfer_problem=new_problem(publication_mode="assisted"),
        )
    )

    assert result["help_level"] >= 1
    assert result["transfer_problem"]["help_level"] >= 1
    assert leaked in publication_text(result)
    support = result["_publication_support"][0]
    assert support["target"] == "new"
    assert support["content"] == leaked
    assert "model_structure" in support["exposure_types"]


def test_independent_publication_suppresses_focus_and_citations_but_keeps_private_analysis():
    original = teaching(
        action="transfer",
        focus="先扣45再除12",
        source_ids=["worked:deduct-fixed-fee"],
        analysis={
            "summary": "图书钱加快递费等于总花费。",
            "reference_solution": "PRIVATE",
        },
        transfer_problem=new_problem(),
    )
    result = prepare_publication(original)

    assert result["focus"] == ""
    assert result["source_ids"] == []
    assert result["analysis"] == original["analysis"]
    discarded = result["_publication_audit"]["suppressed_independent_fields"]
    assert discarded["focus"] == original["focus"]
    assert discarded["source_ids"] == original["source_ids"]
    assert result["_publication_support"] == []


def test_mixed_worked_example_cannot_claim_zero_help_for_attached_new_problem():
    result = prepare_publication(
        teaching(
            action="worked_example",
            content="原题先求两个量的关系。这道新题也先减去固定费用。",
            help_level=3,
            transfer_problem=new_problem(),
        )
    )

    assert result["transfer_problem"]["publication_mode"] == "assisted"
    assert result["transfer_problem"]["help_level"] >= 1
    assert result["question"] == MIXED_INVITATION
    current, new = result["_publication_support"]
    assert current["target"] == "current" and current["help_level"] == 3
    assert new["target"] == "new" and new["help_level"] >= 1
    assert "mixed_publication" in new["exposure_types"]
    assert result["_publication_audit"]["mixed_independent_request"] is True


def test_separate_guidance_on_new_problem_is_published_and_recorded():
    result = prepare_publication(
        teaching(
            action="worked_example",
            content="原题完整讲解。",
            transfer_problem=new_problem(
                publication_mode="assisted",
                teaching_content="新题提示：把固定费用单独表示。",
            ),
        )
    )

    current, new = result["_publication_support"]
    assert current["content"] == "原题完整讲解。"
    assert "新题提示" not in current["content"]
    assert "新题提示：把固定费用单独表示。" in new["content"]
    text = publication_text(result)
    assert text.count("新题提示：把固定费用单独表示。") == 1
    assert text.index(result["transfer_problem"]["stem"]) < text.index("新题提示")


def test_model_annotation_needs_an_actual_published_excerpt():
    result = prepare_publication(
        teaching(
            content="先尝试另一种表示。",
            support_annotations=[
                {"kind": "worked_step", "excerpt": "不存在的演示步骤"}
            ],
        )
    )

    assert "worked_step" not in result["_publication_support"][0]["exposure_types"]
    assert result["help_level"] >= 1


def test_model_completion_with_structure_remains_assistance():
    result = prepare_publication(
        teaching(
            action="complete",
            content="解题时把固定费用加上随数量变化的费用，就得到总花费。",
            question="",
            response_expectation="none",
        )
    )

    assert result["help_level"] >= 2
    assert "model_structure" in result["_publication_support"][0]["exposure_types"]


def test_server_control_close_does_not_manufacture_a_help_event():
    output = teaching(
        action="complete",
        content="本次讨论结束。",
        question="",
        response_expectation="none",
    )
    # 该内部标志只由服务端添加，模型输出schema不允许该字段。
    output["_server_control_message"] = True
    result = prepare_publication(output)

    assert result["_publication_support"] == []


def test_model_cannot_supply_server_control_message_flag():
    with pytest.raises(ValidationError, match="_server_control_message"):
        teaching(_server_control_message=True)


def test_normalizing_independent_output_twice_preserves_original_audit():
    result = prepare_publication(
        teaching(
            action="transfer",
            content="先区分固定费用。",
            transfer_problem=new_problem(),
        )
    )

    repeated = prepare_publication(result)
    assert repeated["_publication_audit"] == result["_publication_audit"]
    assert repeated["_publication_support"] == []


@pytest.mark.parametrize(
    "stem",
    [
        "每本书12元，共525元。提示：先减去45元。",
        "每本书12元。由12x+45=525可得x=40。求本数。",
        "每本书12元，快递费45元，因此x=40。",
    ],
)
def test_explicit_guidance_or_answer_cannot_be_hidden_in_problem_statement(stem):
    with pytest.raises(ValueError, match="stem只能包含题目条件和目标"):
        validate_transfer_statement(new_problem(stem=stem))


def test_an_equation_can_be_a_problem_condition_without_being_a_solution():
    validate_transfer_statement(new_problem(stem="已知12x+45=525，求x。"))
