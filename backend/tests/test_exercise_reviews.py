"""验证第三次模型判定被业务流程采纳，避免重新退回答案字面比较。"""

import asyncio
from copy import deepcopy
import json

import pytest
from sqlalchemy import func, select

from reference_helpers import audit as read_audit, audits
from app.domains.courses.models import Question
from app.domains.practice.context import public_exercise
from app.agent.tools.contracts import ToolContext
from types import SimpleNamespace
from app.application.agent.runner import AgentRunner
from app.application.agent.tools import TeachingTools
from app.application.learning.subgraphs.practice import generation_graph
from app.domains.practice.service import PracticeService
from app.domains.sessions.models import LearningSession
from test_exercise_context import exercise_database, assert_public


def draft(kind="fill", answer=None):
    return {
        "stem": "将一个整体平均分成两份，其中一份占整体的多少？",
        "question_type": kind,
        "options": (
            {}
            if kind == "fill"
            else {"A": "一半", "B": "三分之一", "C": "二分之一", "D": "四分之一"}
        ),
        "answer": answer or ["1/2"],
        "explanation": "GENERATION_PRIVATE：平均分成两份，每份为二分之一。",
        "concept_ids": ["math:c1"],
        "cognitive_type": "understanding",
    }


def independent(answer=None, *, valid=True):
    return {
        "answer": answer or ["0.5"],
        "explanation": "SOLVER_PRIVATE：用小数表示一半为0.5。",
        "valid": valid,
        "issue": "" if valid else "需要复核单位表述。",
    }


def answer_assessment(answer, *, correctness="correct", consistency="consistent"):
    return {
        "submitted_answer": answer,
        "reason": "核对实际答案栏及对应解析，按原始回答声明的完整或部分范围判断。",
        "correctness": correctness,
        "answer_explanation_consistency": consistency,
    }


def review(verdict="equivalent", answer=None, *, generated=None, solved=None):
    accepted = verdict in {"equivalent", "complementary"}
    return {
        "generation_assessment": answer_assessment(generated or ["1/2"]),
        "independent_assessment": answer_assessment(solved or ["0.5"]),
        "verdict": verdict,
        "answer": (answer or ["0.5"]) if accepted else [],
        "explanation": (
            "MERGED_PRIVATE：分数二分之一与小数0.5表示同一数量，两种解法相互印证。"
            if accepted
            else ""
        ),
        "reason": (
            "两份正确解答数学等价。"
            if accepted
            else "两份结论存在实质冲突，需要重新设计题目。"
        ),
    }


def scripted_service(monkeypatch, responses):
    model = SimpleNamespace(
        settings=SimpleNamespace(llm_model="scripted-review"), calls=[], messages=[], indexed=[],
    )
    exercises = PracticeService(
        AgentRunner(model, TeachingTools(SimpleNamespace())), generation_graph=generation_graph(),
    )
    queue = iter(responses)

    async def chat(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        if payload.get("task") in {"generate", "validate", "reconcile"}:
            model.calls.append(deepcopy(payload))
            model.messages.append(deepcopy(messages))
            expected_task, output = next(queue)
            assert payload["task"] == expected_task
            return {
                "choices": [
                    {"message": {"content": json.dumps(output, ensure_ascii=False)}}
                ],
                "usage": {},
            }
        raise AssertionError(payload)

    monkeypatch.setattr(model, "chat", chat, raising=False)
    return exercises, model


def generate(exercises, *, kind="fill", events=None):
    from app.domains.practice.service import db_session
    with db_session() as db:
        db.get(LearningSession, "mine").active_turn_id = "review-turn"
    async def collect(event, data):
        if events is not None:
            events.append((event, deepcopy(data)))

    return asyncio.run(
        exercises.generate(
            ToolContext(course_id="math", session_id="mine", concept_ids=["math:c1"], request_id="review-operation"),
            "review-turn",
            "understanding",
            collect,
            assignment={"index": 2, "question_type": kind},
        )
    )


@pytest.mark.parametrize(
    "kind,generated,solved,canonical",
    [
        ("fill", ["1/2"], ["0.5"], ["0.5"]),
        ("fill", ["36平方厘米"], ["36"], ["36 平方厘米"]),
        ("choice", ["A"], ["一半"], ["A"]),
        ("multi_choice", ["A", "C"], ["A,C"], ["A", "C"]),
    ],
)
def test_semantically_equivalent_answers_are_reviewed_and_canonical_output_is_saved(
    exercise_database, monkeypatch, kind, generated, solved, canonical
):
    original = draft(kind, generated)
    # 选择题仍使用四个不同的选项文字，不能把格式问题冒充语义差异。
    original["options"] = (
        {}
        if kind == "fill"
        else {"A": "一半", "B": "三分之一", "C": "另外一种正确表述", "D": "四分之一"}
    )
    exercises, model = scripted_service(
        monkeypatch,
        [
            ("generate", original),
            ("validate", independent(solved)),
            ("reconcile", review(answer=canonical, generated=generated, solved=solved)),
        ],
    )
    events = []
    contract, _ = generate(exercises, kind=kind, events=events)
    assert [payload["task"] for payload in model.calls] == [
        "generate",
        "validate",
        "reconcile",
    ]
    assert "answer" not in model.calls[1] and "explanation" not in model.calls[1]
    assert model.calls[2]["generation_solution"]["answer"] == generated
    assert model.calls[2]["independent_solution"]["answer"] == solved
    assert contract["answer"] == canonical and contract["explanation"].startswith(
        "MERGED_PRIVATE"
    )
    assert contract["stem"] == original["stem"]
    assert_public(public_exercise(contract, "math"))
    quality = [data for event, data in events if event == "exercise_quality_checked"]
    assert len(quality) == 1 and quality[0]["verdict"] == "equivalent"
    assert_public(quality)
    assert "MERGED_PRIVATE" not in json.dumps(events, ensure_ascii=False)
    with exercise_database() as db:
        row = db.get(Question, contract["id"])
        assert row.validation["semantic_review_passed"]
        assert "independent_solution_agreed" not in row.validation
        audit = read_audit(db, row.validation["generation_attempt_id"])
        assert (
            audit.status == "accepted"
            and audit.task_index == 2
            and audit.attempt_number == 1
        )
        assert audit.details["generation"]["output"]["answer"] == generated
        assert audit.details["independent"]["output"]["answer"] == solved
        assert audit.details["review"]["output"]["answer"] == canonical
        ids = [
            audit.details[key]["run_id"]
            for key in ("generation", "independent", "review")
        ]
        assert len(set(ids)) == 3


@pytest.mark.parametrize("same_answer", [True, False])
def test_complementary_solutions_always_get_a_third_call_and_use_merged_explanation(
    exercise_database, monkeypatch, same_answer
):
    exercises, model = scripted_service(
        monkeypatch,
        [
            ("generate", draft()),
            ("validate", independent(["1/2"] if same_answer else ["0.5"])),
            (
                "reconcile",
                review(
                    "complementary",
                    ["二分之一"],
                    solved=["1/2"] if same_answer else ["0.5"],
                ),
            ),
        ],
    )
    contract, _ = generate(exercises)
    assert len(model.calls) == 3
    assert contract["answer"] == ["二分之一"] and contract["explanation"].startswith(
        "MERGED_PRIVATE"
    )


def test_conflict_retries_once_with_full_feedback_then_publishes_only_reviewed_answer(
    exercise_database, monkeypatch
):
    exercises, model = scripted_service(
        monkeypatch,
        [
            ("generate", draft()),
            ("validate", independent(["0.6"])),
            ("reconcile", review("conflict", solved=["0.6"])),
            ("generate", draft()),
            ("validate", independent()),
            ("reconcile", review()),
        ],
    )
    contract, _ = generate(exercises)
    assert [payload["task"] for payload in model.calls] == [
        "generate",
        "validate",
        "reconcile",
    ] * 2
    retry = model.calls[3]
    assert "实质冲突" in retry["previous_issue"]
    assert retry["previous_attempt"]["generation_solution"]["answer"] == ["1/2"]
    assert retry["previous_attempt"]["independent_solution"]["answer"] == ["0.6"]
    assert contract["answer"] == ["0.5"] and not model.indexed
    with exercise_database() as db:
        records = audits(db)
        assert [record.status for record in records] == ["rejected", "accepted"]
        assert [record.attempt_number for record in records] == [1, 2]


def test_reviewed_practice_does_not_require_shared_search_services(exercise_database, monkeypatch):
    exercises, model = scripted_service(monkeypatch, [
        ("generate", draft()), ("validate", independent()), ("reconcile", review()),
    ])
    async def unavailable(*args, **kwargs):
        raise AssertionError("生成练习不应调用共享题库向量或ES服务")
    model.embed = unavailable
    model.bulk_index = unavailable
    contract, _ = generate(exercises)
    assert contract["answer"] == ["0.5"] and contract["generated"]
    assert len(model.calls) == 3 and not model.indexed


def test_correct_partial_conclusions_can_be_combined_without_changing_the_question(
    exercise_database, monkeypatch
):
    original = draft(answer=["整数部分：1个"])
    original["stem"] = (
        "将三个苹果平均分给两个人，请用整数部分加分数部分的形式填写每人分得的苹果数。"
    )
    original["explanation"] = "先给每人一个苹果，还剩一个等待分配。"
    solved = independent(["分数部分：1/2个"])
    solved["explanation"] = "余下一个苹果对半分，每人再得到半个。"
    combined = review(
        "complementary",
        ["1+1/2个"],
        generated=["整数部分：1个"],
        solved=["分数部分：1/2个"],
    )
    combined["explanation"] = "先给每人一个，再把余下一个对半分，所以每人得到1+1/2个。"
    exercises, model = scripted_service(
        monkeypatch,
        [
            ("generate", original),
            ("validate", solved),
            ("reconcile", combined),
        ],
    )
    contract, _ = generate(exercises)
    assert contract["answer"] == ["1+1/2个"] and contract["stem"] == original["stem"]
    assert contract["explanation"] == combined["explanation"]
    assert len(model.calls) == 3


@pytest.mark.parametrize(
    "verdict", ["repair_required", "conflict", "invalid", "uncertain"]
)
def test_two_rejections_stop_without_saving_or_indexing_a_question(
    exercise_database, monkeypatch, verdict
):
    exercises, model = scripted_service(
        monkeypatch,
        [
            ("generate", draft()),
            ("validate", independent()),
            ("reconcile", review(verdict)),
            ("generate", draft()),
            ("validate", independent()),
            ("reconcile", review(verdict)),
        ],
    )
    with pytest.raises(ValueError, match="语义复核未通过"):
        generate(exercises)
    assert len(model.calls) == 6 and not model.indexed
    with exercise_database() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(Question)
                .where(Question.generated.is_(True))
            )
            == 0
        )
        assert (
            len(audits(db)) == 2
        )


def test_independent_valid_flag_is_advisory_for_the_third_review(
    exercise_database, monkeypatch
):
    exercises, model = scripted_service(
        monkeypatch,
        [
            ("generate", draft()),
            ("validate", independent(valid=False)),
            ("reconcile", review()),
        ],
    )
    contract, _ = generate(exercises)
    assert model.calls[2]["independent_solution"]["valid"] is False
    assert contract["answer"] == ["0.5"]


def test_review_output_still_needs_an_executable_answer_contract(
    exercise_database, monkeypatch
):
    original = draft("choice", ["A"])
    original["options"] = {
        "A": "一半",
        "B": "三分之一",
        "C": "三分之二",
        "D": "四分之一",
    }
    exercises, model = scripted_service(
        monkeypatch,
        [
            ("generate", original),
            ("validate", independent(["A"])),
            (
                "reconcile",
                review(answer=["不存在的选项"], generated=["A"], solved=["A"]),
            ),
            ("reconcile", review(answer=["A"], generated=["A"], solved=["A"])),
        ],
    )
    contract, _ = generate(exercises, kind="choice")
    assert contract["answer"] == ["A"]
    assert [payload["task"] for payload in model.calls] == [
        "generate",
        "validate",
        "reconcile",
        "reconcile",
    ]


def test_bad_draft_format_is_recorded_and_retried_before_model_review(
    exercise_database, monkeypatch
):
    malformed = draft()
    malformed["answer"] = ["1/2", "0.5"]
    exercises, model = scripted_service(
        monkeypatch,
        [
            ("generate", malformed),
            ("generate", draft()),
            ("validate", independent()),
            ("reconcile", review()),
        ],
    )
    generate(exercises)
    assert [payload["task"] for payload in model.calls] == [
        "generate",
        "generate",
        "validate",
        "reconcile",
    ]
    with exercise_database() as db:
        rejected = next(a for a in audits(db) if a.status == "rejected")
        assert (
            rejected.details["independent"] is None
            and rejected.details["review"] is None
        )
        assert "单空" in rejected.reason


def test_wrong_choice_acceptance_is_repaired_in_same_review_run_before_new_candidate(
    exercise_database, monkeypatch
):
    original = draft("choice", ["A"])
    original["options"] = {
        "A": "一半",
        "B": "三分之一",
        "C": "三分之二",
        "D": "四分之一",
    }
    incorrect_accept = review(answer=["A"], generated=["A"], solved=["B"])
    rejected = review("repair_required", generated=["A"], solved=["B"])
    rejected["independent_assessment"] = answer_assessment(
        ["B"], correctness="incorrect", consistency="inconsistent"
    )
    exercises, model = scripted_service(
        monkeypatch,
        [
            ("generate", original),
            ("validate", independent(["B"])),
            ("reconcile", incorrect_accept),
            ("reconcile", rejected),
            ("generate", original),
            ("validate", independent(["A"])),
            ("reconcile", review(answer=["A"], generated=["A"], solved=["A"])),
        ],
    )
    contract, _ = generate(exercises, kind="choice")
    assert contract["answer"] == ["A"]
    assert [payload["task"] for payload in model.calls] == [
        "generate",
        "validate",
        "reconcile",
        "reconcile",
        "generate",
        "validate",
        "reconcile",
    ]
    assert "完整选择题答案集合不一致" in model.messages[3][-1]["content"]
    with exercise_database() as db:
        attempts = audits(db)
        assert [row.status for row in attempts] == ["rejected", "accepted"]
        assert attempts[0].details["review"]["output"]["verdict"] == "repair_required"
        assert attempts[0].details["independent"]["output"]["answer"] == ["B"]
        assert attempts[0].details["review"]["output"]["answer"] == []
        # 首次复核的两次模型输出属于同一候选、同一run，不伪造新的生成尝试。
        from app.infrastructure.persistence.execution_models import AgentRun

        assert db.scalar(select(func.count()).select_from(AgentRun)) == 6
