"""验证综合题局部归因、冻结评分与原始事件去重，不访问外部模型。"""

import asyncio
from copy import deepcopy
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.domains.assessment.models import EvidenceConceptLink
from app.domains.sessions.models import LearningSession
from app.domains.students.models import StudentState
from app.domains.assessment.evidence import (
    canonical_item_id, freeze_grading_packet, load_mastery_evidence, persist_concept_links,
)
from app.domains.students.mastery import apply_ledger_evidence
from app.domains.students.service import StudentService


def contract(*, single=False, **updates):
    return {"id": "math:composite", "course_id": "math", "version": 1,
        "stem": "展开2(x+1)，再说明为何等式两边同减2。", "options": {}, "question_type": "fill",
        "answer": ["2x+2"], "source_ref": "synthetic-test", "cognitive_type": "understanding",
        "concept_ids": ["math:distribution"] if single else ["math:distribution", "math:equality"],
        "assessment_criteria": [
            {"id": "distribution", "concept_id": "math:distribution", "dimension": "advanced", "description": "展开时乘到括号内每一项。"},
            {"id": "equality", "concept_id": "math:equality", "dimension": "advanced", "description": "说明两边执行相同减法保持相等。"},
        ], **updates}


ANSWER = "2(x+1)=2x+2。下一步我不会解释。"


def diagnosis(*, observed="correct", **updates):
    return {"result": "incorrect", "score": 0.0, "confidence": 0.95, "level": "advanced",
        "suggested_delta": -0.25, "misconception": "整题错因不能广播", "concept_results": [
            {"concept_id": "math:distribution", "dimension": "advanced", "criterion_id": "distribution",
             "result": observed, "score": 1.0 if observed == "correct" else 0.0, "confidence": 0.95,
             "source_span_refs": [{"quote": "2(x+1)=2x+2"}], "rationale": "当前展开正确。"}
        ], **updates}


def freeze(*, question=None, answer=ANSWER, proposed=None, assisted=False, version=None):
    question = question or contract()
    return freeze_grading_packet(question, answer, proposed or diagnosis(), question["concept_ids"], assisted,
        {"hint_count": 1 if assisted else 0}, "grading-run", source_version=version)


@pytest.fixture
def database():
    from app.infrastructure.persistence.database import Base

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        db.add(LearningSession(id="s1", student_id="student", course_id="math", title="局部归因", goal="局部验证"))
        db.flush()
        yield db
    engine.dispose()


def save(db, packet, *, attempt_id="a1", answer=ANSWER, question_id="math:composite"):
    attempt = SimpleNamespace(id=attempt_id, question_id=question_id, answer_text=answer)
    session = db.get(LearningSession, "s1")
    persist_concept_links(db, attempt, session, packet)
    evidence = load_mastery_evidence(
        db, packet=packet, student_id="student",
        concept_ids=["math:distribution", "math:equality"], attempt_id=attempt_id,
    )
    return apply_ledger_evidence(
        db, evidence_by_concept=evidence, student_id="student", attempt_id=attempt_id,
    )


def test_composite_does_not_broadcast_overall_incorrect_to_correct_local_skill(database):
    packet = freeze()
    result = {item["concept_id"]: item for item in packet["concepts"]}
    assert result["math:distribution"]["result"] == "correct"
    assert result["math:equality"]["result"] == "not_observed"
    changes = save(database, packet)
    assert [item["concept_id"] for item in changes] == ["math:distribution"]
    assert database.get(StudentState, "student:math:distribution").mastery["advanced"] == 0.5
    assert database.get(StudentState, "student:math:equality") is None


@pytest.mark.parametrize("change", [
    {"criterion_id": "model-invented-criterion"},
    {"concept_id": "math:equality"},
    {"dimension": "proficient"},
    {"source_span_refs": [{"quote": "原答没有的完美解释"}]},
    {"source_span_refs": [{"quote": "2(x+1)=2x+2", "start": 2, "end": 12}]},
])
def test_unsupported_local_candidate_stays_unobserved(change, database):
    proposed = diagnosis()
    proposed["concept_results"][0].update(change)
    packet = freeze(proposed=proposed)
    assert not any(item["eligible_for_mastery"] for item in packet["concepts"])
    assert save(database, packet) == []


def test_composite_without_contract_criteria_retains_overall_score_only(database):
    packet = freeze(question=contract(assessment_criteria=[]))
    assert packet["overall"]["result"] == "incorrect"
    assert {item["result"] for item in packet["concepts"]} == {"not_observed"}
    assert save(database, packet) == []


def test_selecting_one_target_from_composite_is_not_single_concept_shortcut():
    question = contract(assessment_criteria=[])
    packet = freeze_grading_packet(question, ANSWER, diagnosis(), ["math:distribution"], False)
    assert packet["concepts"][0]["result"] == "not_observed"


def test_single_concept_whole_item_is_direct_and_assistance_remains_descriptive(database):
    question = contract(single=True)
    packet = freeze(question=question, assisted=True)
    assert packet["concepts"][0]["attribution"] == "direct"
    assert not packet["concepts"][0]["eligible_for_mastery"]
    assert save(database, packet) == []
    link = database.scalar(select(EvidenceConceptLink))
    assert link.help_snapshot["assisted"] and link.source_span_refs[0]["quote"] == ANSWER
    assert link.observed_result == "incorrect"


def test_multiple_criteria_reduce_to_one_contribution_and_replay_is_idempotent(database):
    question = contract()
    question["assessment_criteria"].append({"id": "constant", "concept_id": "math:distribution", "dimension": "advanced", "description": "常数项也需相乘。"})
    proposed = diagnosis()
    proposed["concept_results"].append({**deepcopy(proposed["concept_results"][0]), "criterion_id": "constant"})
    packet = freeze(question=question, proposed=proposed)
    assert len(save(database, packet)) == 1
    assert save(database, packet) == []
    state = database.get(StudentState, "student:math:distribution")
    assert state.evidence_ids == ["a1"] and len(state.evidence_questions) == 1
    assert state.mastery["advanced"] == 0.5
    assert database.scalar(select(func.count()).select_from(EvidenceConceptLink)) == 3


def test_grading_revision_replaces_contribution_instead_of_adding(database):
    from app.domains.memory.scopes.dependencies import scope_key
    from app.domains.memory.scopes.models import ScopeMemory
    from app.domains.memory.models import MemoryFact

    old = freeze(proposed=diagnosis(observed="incorrect"))
    save(database, old)
    assert database.get(StudentState, "student:math:distribution").mastery["advanced"] == 0.0
    revised = freeze(version="contract:1:grading:2")
    save(database, revised)
    state = database.get(StudentState, "student:math:distribution")
    assert state.mastery["advanced"] == 0.5
    assert state.evidence_ids == ["a1"] and len(state.evidence_questions) == 1
    assert database.scalar(select(func.count()).select_from(EvidenceConceptLink)) == 2
    assert all(row.source_version == "contract:1:grading:2" for row in database.scalars(select(EvidenceConceptLink)))
    # 原始历史保留，但旧版本来源不再有资格复活到概念摘要。
    assert database.scalar(select(func.count()).select_from(MemoryFact)) == 4
    scope = database.get(ScopeMemory, scope_key("student", "concept", "math", "math:distribution"))
    assert any(ref.endswith(":v1") for ref in scope.blocked_source_refs)


def test_revision_withdrawal_recalculates_old_contribution(database):
    save(database, freeze())
    revised = freeze(proposed=diagnosis(concept_results=[]), version="contract:1:grading:2")
    save(database, revised)
    state = database.get(StudentState, "student:math:distribution")
    assert state.mastery["advanced"] is None and state.evidence_ids == []


def test_canonical_item_deduplicates_rewording_of_layout_and_new_version(database):
    first = contract()
    second = contract(id="math:new-id", version=99, stem="展开 2(x+1)，再说明为何等式两边同减2。", explanation="修改解析")
    assert canonical_item_id(first) == canonical_item_id(second)
    save(database, freeze(question=first))
    assert save(database, freeze(question=second), attempt_id="a2", question_id="math:new-id") == []
    assert database.get(StudentState, "student:math:distribution").evidence_ids == ["a1"]


def test_diagnosis_cannot_rewrite_frozen_local_result(database):
    packet = freeze()
    packet["concepts"][0]["score"] = 0.0
    with pytest.raises(ValueError, match="冻结评分回执已被修改"):
        save(database, packet)


def test_legacy_multiconcept_entrypoint_refuses_broadcast_without_packet():
    assert StudentService.apply_evidence(object(), concept_ids=["c1", "c2"], attempt_id="a", question_id="q",
        diagnosis={"result": "correct", "confidence": 1}, assisted=False) == []


@pytest.fixture
def service_database(monkeypatch):
    from app.infrastructure.persistence.database import Base

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    @contextmanager
    def scope():
        with Session(engine, expire_on_commit=False) as db:
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    monkeypatch.setattr("app.domains.assessment.service.db_session", scope)
    monkeypatch.setattr("app.domains.assessment.batch.db_session", scope)
    yield scope
    engine.dispose()


def assessment_service(outputs, calls):
    from app.domains.assessment.service import AssessmentService

    async def run(*args, **kwargs):
        payload = args[2]
        assert kwargs["context_purpose"] == "grading" and kwargs["use_tools"] is False
        assert "student_state" not in payload and "prediction" not in payload
        calls.append(payload)
        return {"output": outputs, "run_id": "grading-test"}

    service = AssessmentService(SimpleNamespace(run=run))
    service.catalog = SimpleNamespace(course=lambda _: None, concept=lambda *_: None)
    # 若回归成评分前读取历史标签，测试应立即失败。
    service.student.profile = lambda *_: (_ for _ in ()).throw(AssertionError("独立评分不得读取学生历史"))
    return service


async def emit(*_args):
    pass


def test_real_single_service_freezes_local_receipt_and_replays(service_database):
    from app.domains.assessment.models import Attempt

    question = contract()
    with service_database() as db:
        db.add(LearningSession(id="s", student_id="student", course_id="math", title="综合题", goal="展开并解释", active_turn_id="turn",
            pending_question_id=question["id"], pending_context={"contract": question,
            "target_concept_ids": question["concept_ids"], "cognitive_type": "understanding", "prediction": {"predicted_correct": False}, "assisted": False}))
    calls = []
    proposed = {**diagnosis(), "feedback": "先保留正确展开，再说明等式性质。", "reflection": "局部依据。", "next_action": "remediate"}
    service = assessment_service(proposed, calls)
    response = asyncio.run(service.assess("s", ANSWER, "submit-local", "turn", emit))
    replay = asyncio.run(service.assess("s", ANSWER, "submit-local", "turn", emit))
    assert replay["replayed"] and len(calls) == 1
    assert [item["concept_id"] for item in response["state_updates"]] == ["math:distribution"]
    with service_database() as db:
        saved = db.scalar(select(Attempt))
        assert saved.assessment["grading_packet"]["overall"]["result"] == "incorrect"
        assert db.scalar(select(func.count()).select_from(EvidenceConceptLink)) == 2
        assert db.get(StudentState, "student:math:equality") is None


def test_real_batch_service_keeps_distinct_local_results_and_one_event_per_dimension(service_database):
    from app.domains.assessment.models import Attempt
    from app.domains.assessment.schemas import BatchAnswerRequest
    from app.domains.assessment.batch import BatchAssessmentService

    questions = [contract(id="math:q1"), contract(id="math:q2", version=2)]
    batch = {"id": "batch-1", "items": [{"contract": question, "target_concept_ids": question["concept_ids"],
        "cognitive_type": "understanding", "assisted": False} for question in questions]}
    with service_database() as db:
        db.add(LearningSession(id="s", student_id="student", course_id="math", title="综合练习", goal="局部验证",
            active_turn_id="turn", pending_context={"exercise_batch": batch}))
    proposed = {**diagnosis(), "feedback": "展开正确，解释待补。", "reflection": "当前原答局部依据。", "next_action": "remediate"}
    outputs = {"items": [{"question_id": question["id"], "diagnosis": deepcopy(proposed)} for question in questions],
        "summary": "两个相同实质任务保留各自记录，未观察的技能仍需诊断。", "next_action": "remediate"}
    calls = []
    service = BatchAssessmentService(assessment_service(outputs, calls))
    request = BatchAnswerRequest(batch_id="batch-1", answers=[{"question_id": question["id"], "answer": ANSWER} for question in questions], request_id="batch-submit", wait_id="domain-batch-1")
    response = asyncio.run(service.assess("s", request, "turn", emit))
    replay = asyncio.run(service.assess("s", request, "turn", emit))
    assert len(calls) == 1 and replay["replayed"]
    assert response["items"][0]["state_updates"] and not response["items"][1]["state_updates"]
    with service_database() as db:
        assert db.scalar(select(func.count()).select_from(Attempt)) == 2
        assert db.scalar(select(func.count()).select_from(EvidenceConceptLink)) == 4
        state = db.get(StudentState, "student:math:distribution")
        assert len(state.evidence_ids) == len(state.evidence_questions) == 1
        assert db.get(StudentState, "student:math:equality") is None


def test_attempt_links_state_and_memory_facts_rollback_as_one_unit(service_database, monkeypatch):
    from app.domains.assessment.models import Attempt
    from app.domains.memory.models import MemoryFact

    question = contract()
    with service_database() as db:
        db.add(LearningSession(id="s", student_id="student", course_id="math", title="综合题", goal="局部验证", active_turn_id="turn",
            pending_question_id=question["id"], pending_context={"contract": question,
            "target_concept_ids": question["concept_ids"], "cognitive_type": "understanding", "assisted": False}))
    service = assessment_service({**diagnosis(), "feedback": "本次评分", "reflection": "局部依据", "next_action": "continue"}, [])

    def fail(*_args):
        raise RuntimeError("模拟同事务事实写入故障")

    monkeypatch.setattr("app.domains.memory.recording.facts.record_evidence_fact", fail)
    with pytest.raises(RuntimeError, match="同事务事实写入故障"):
        asyncio.run(service.assess("s", ANSWER, "submit-rollback", "turn", emit))
    with service_database() as db:
        for model in (Attempt, EvidenceConceptLink, StudentState, MemoryFact):
            assert db.scalar(select(func.count()).select_from(model)) == 0
        assert db.get(LearningSession, "s").pending_question_id == question["id"]


def test_revision_recalculates_removed_concept_even_when_new_targets_exclude_it(database):
    save(database, freeze())
    question = contract(concept_ids=["math:equality"])
    revised = freeze(
        question=question,
        proposed=diagnosis(concept_results=[]),
        version="contract:2:grading:2",
    )
    session = database.get(LearningSession, "s1")
    attempt = SimpleNamespace(id="a1", question_id=question["id"], answer_text=ANSWER)
    persist_concept_links(database, attempt, session, revised)
    evidence = load_mastery_evidence(
        database, packet=revised, student_id="student",
        concept_ids=["math:equality"], attempt_id="a1",
    )
    changes = apply_ledger_evidence(
        database, evidence_by_concept=evidence, student_id="student", attempt_id="a1",
    )
    assert "math:distribution" in {item["concept_id"] for item in changes}
    state = database.get(StudentState, "student:math:distribution")
    assert state.mastery["advanced"] is None and state.evidence_ids == []


def test_student_write_failure_rolls_back_attempt_ledger_memory_and_session(service_database, monkeypatch):
    from app.domains.assessment.models import Attempt
    from app.domains.memory.models import MemoryFact

    question = contract()
    with service_database() as db:
        db.add(LearningSession(
            id="s", student_id="student", course_id="math", title="事务回滚", goal="局部验证", active_turn_id="turn",
            pending_question_id=question["id"], pending_context={
                "contract": question, "target_concept_ids": question["concept_ids"],
                "cognitive_type": "understanding", "assisted": False,
            },
        ))
    service = assessment_service({
        **diagnosis(), "feedback": "本次评分", "reflection": "局部依据", "next_action": "continue",
    }, [])
    original = service.student.apply_evidence

    def fail_after_student_write(db, **kwargs):
        changes = original(db, **kwargs)
        assert changes and db.get(StudentState, "student:math:distribution") is not None
        # 在学生状态已经写入同一事务后失败，不能留下部分评估结果。
        raise RuntimeError("模拟掌握状态写入后故障")

    monkeypatch.setattr(service.student, "apply_evidence", fail_after_student_write)
    with pytest.raises(RuntimeError, match="掌握状态写入后故障"):
        asyncio.run(service.assess("s", ANSWER, "student-rollback", "turn", emit))
    with service_database() as db:
        for model in (Attempt, EvidenceConceptLink, StudentState, MemoryFact):
            assert db.scalar(select(func.count()).select_from(model)) == 0
        session = db.get(LearningSession, "s")
        assert session.pending_question_id == question["id"]
        assert session.pending_context["contract"] == question


def test_ledger_reads_interleave_with_student_state_locks_by_concept(database, monkeypatch):
    packet = freeze()
    attempt = SimpleNamespace(id="ordered", question_id="math:composite", answer_text=ANSWER)
    persist_concept_links(database, attempt, database.get(LearningSession, "s1"), packet)
    events = []
    original_scalars, original_get = database.scalars, database.get

    def observed_scalars(statement, *args, **kwargs):
        descriptions = getattr(statement, "column_descriptions", [])
        if len(descriptions) == 1 and descriptions[0]["expr"] is EvidenceConceptLink:
            params = statement.compile().params
            concept = next(value for key, value in params.items() if key.startswith("concept_id"))
            events.append(("ledger_select", concept))
        return original_scalars(statement, *args, **kwargs)

    def observed_get(entity, identifier, *args, **kwargs):
        if entity is StudentState:
            # SQLite不提供行锁；检查真实服务仍请求FOR UPDATE及其与账本读取的顺序。
            assert kwargs.get("with_for_update") is True
            events.append(("state_lock", identifier))
        return original_get(entity, identifier, *args, **kwargs)

    monkeypatch.setattr(database, "scalars", observed_scalars)
    monkeypatch.setattr(database, "get", observed_get)
    evidence = load_mastery_evidence(
        database, packet=packet, student_id="student",
        concept_ids=["math:equality", "math:distribution"], attempt_id="ordered",
    )
    assert events == []
    apply_ledger_evidence(
        database, evidence_by_concept=evidence, student_id="student", attempt_id="ordered",
    )
    assert events == [
        ("ledger_select", "math:distribution"),
        ("state_lock", "student:math:distribution"),
        ("ledger_select", "math:equality"),
        ("state_lock", "student:math:equality"),
    ]
