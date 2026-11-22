"""复核协议守住实际答案栏，数学语义仍交由显式模型评估。"""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.domains.practice.schemas import ExerciseDraft, SolutionCheck, SolutionReview
from app.domains.practice.answer_contracts import choice_answer_labels, review_contract_issue

OPTIONS = {"A": "190 本", "B": "200 本", "C": "210 本", "D": "180 本"}


def assessment(answer, *, correctness="correct", consistency="consistent"):
    return {
        "submitted_answer": answer,
        "reason": "逐项核对真实答案栏和该份解析的结论。",
        "correctness": correctness,
        "answer_explanation_consistency": consistency,
    }


def checked_review(generated, solved, canonical, *, verdict="equivalent"):
    return {
        "generation_assessment": assessment(generated),
        "independent_assessment": assessment(solved),
        "reason": "两份输入答案均已核对。",
        "explanation": "公开推导及代入检验一致。",
        "verdict": verdict,
        "answer": canonical,
    }


def test_explanation_precedes_final_answer_in_all_generation_contracts():
    for schema in (ExerciseDraft, SolutionCheck):
        fields = list(schema.model_json_schema()["properties"])
        assert fields.index("explanation") < fields.index("answer")
    fields = list(SolutionReview.model_json_schema()["properties"])
    assert fields[-1] == "answer"
    assert fields.index("reason") < fields.index("verdict")
    assert fields.index("generation_assessment") < fields.index("verdict")
    assert fields.index("independent_assessment") < fields.index("verdict")


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (["A"], ("A",)),
        (["a"], ("A",)),
        (["190 本"], ("A",)),
        (["C", "A"], ("A", "C")),
        (["A,C"], ("A", "C")),
        (["A，C"], ("A", "C")),
        (["AC"], ("A", "C")),
        (["190 本", "210 本"], ("A", "C")),
        (["A,A"], None),
        (["A", "A"], None),
        (["答案应该是A"], None),
        (["190"], None),
        (["1+1"], None),
        (["E"], None),
        ([], None),
    ],
)
def test_choice_identity_mapping_does_not_solve_or_extract_from_explanation(
    answer, expected
):
    assert choice_answer_labels(OPTIONS, answer) == expected


@pytest.mark.parametrize("verdict", ["equivalent", "complementary"])
@pytest.mark.parametrize(
    ("correctness", "consistency"),
    [
        ("incorrect", "consistent"),
        ("correct", "inconsistent"),
        ("uncertain", "consistent"),
        ("correct", "uncertain"),
    ],
)
def test_acceptance_requires_both_answers_correct_and_consistent(
    verdict, correctness, consistency
):
    output = checked_review(["1/2"], ["0.5"], ["0.5"], verdict=verdict)
    output["independent_assessment"] = assessment(
        ["0.5"], correctness=correctness, consistency=consistency
    )
    with pytest.raises(ValidationError, match="不能接受"):
        SolutionReview.model_validate(output)


def test_repair_required_is_not_an_accepted_correction():
    output = checked_review(["162"], ["102"], [], verdict="repair_required")
    output["generation_assessment"] = assessment(
        ["162"], correctness="incorrect", consistency="inconsistent"
    )
    output["independent_assessment"] = assessment(
        ["102"], correctness="incorrect", consistency="inconsistent"
    )
    assert SolutionReview.model_validate(output).answer == []
    output["answer"] = ["186"]
    with pytest.raises(ValidationError, match="需要修复"):
        SolutionReview.model_validate(output)


@pytest.mark.parametrize("kind", ["choice", "multi_choice"])
def test_different_complete_choice_sets_cannot_be_accepted_even_with_false_correct_flags(
    kind,
):
    original = {"question_type": kind, "options": OPTIONS, "answer": ["A"]}
    solved = {"answer": ["C"]}
    output = checked_review(["A"], ["C"], ["A"])
    assert "完整选择题答案集合不一致" in review_contract_issue(original, solved, output)
    output["verdict"] = "repair_required"
    output["answer"] = []
    assert review_contract_issue(original, solved, output) == ""


def test_review_cannot_change_agreed_choice_label_to_another_answer():
    original = {"question_type": "choice", "options": OPTIONS, "answer": ["A"]}
    output = checked_review(["A"], ["190 本"], ["C"])
    assert review_contract_issue(original, {"answer": ["190 本"]}, output)


def test_original_option_text_and_comma_labels_keep_compatible_identity():
    original = {
        "question_type": "multi_choice",
        "options": OPTIONS,
        "answer": ["A", "C"],
    }
    solved = {"answer": ["A,C"]}
    output = checked_review(original["answer"], solved["answer"], ["C", "A"])
    assert review_contract_issue(original, solved, output) == ""


def test_fraction_and_decimal_fill_answers_are_not_rejected_by_string_difference():
    original = {"question_type": "fill", "options": {}, "answer": ["1/2"]}
    solved = {"answer": ["0.5"]}
    output = checked_review(original["answer"], solved["answer"], ["二分之一"])
    before = deepcopy((original, solved, output))
    assert review_contract_issue(original, solved, output) == ""
    assert (original, solved, output) == before


def test_reported_answer_cannot_be_replaced_with_a_correct_number_from_explanation():
    original = {"question_type": "fill", "options": {}, "answer": ["162"]}
    solved = {"answer": ["102"]}
    output = checked_review(["186"], ["186"], ["186"])
    assert "submitted_answer必须原样回执" in review_contract_issue(
        original, solved, output
    )


@pytest.mark.parametrize("answer", [[], [""]])
def test_missing_independent_answer_cannot_be_accepted_as_correct_partial(answer):
    original = {"question_type": "fill", "options": {}, "answer": ["1/2"]}
    output = checked_review(original["answer"], answer, ["1/2"])
    assert "实际非空答案" in review_contract_issue(original, {"answer": answer}, output)
