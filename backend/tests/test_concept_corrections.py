from app.domains.memory.models import MemoryJob
from app.domains.memory.scopes.models import SessionMemoryExtraction
from reference_helpers import put_scope
"""概念纠正是受作用域约束的自述，不是正式评估或无帮助作答。"""

import asyncio
import json

import pytest
from sqlalchemy import func, select

from test_concept_memory import Model, claim, database
from app.domains.memory.concepts.service import BusinessMemoryPipeline
from app.domains.memory.recording.facts import record_fact
from app.domains.memory.concepts.observations import Extraction, ObservationExtractor
from app.domains.memory.service import MemoryService
from app.domains.students.models import StudentState
from app.domains.memory.scopes.models import ScopeMemory
from app.domains.memory.scopes.dependencies import scope_key


def correction(**changes):
    return {"seq": 1, "source_ref": "correction:one", "kind": "correction", "payload": {
        "instruction": "请纠正分配律记录：上次不是漏乘，而是抄写错误，需要新题验证。",
        "scope_kind": "concept", "concept_id": "c1", "course_id": "math",
        "explicit_student_request": True, **changes}}


def candidate(fact, *, concept_id="c1", source_kind="model_observation", help_condition="recorded_unassisted", quote=None):
    return Extraction.model_validate({"observations": [{"concept_id": concept_id, "claims": [
        claim(fact["source_ref"], quote or fact["payload"]["instruction"], "学生澄清上次错误归因，待新题验证",
              source_kind=source_kind, help_condition=help_condition)]}]})


def test_valid_concept_correction_becomes_self_report_with_unknown_help():
    fact = correction()
    result, _ = ObservationExtractor.validate(candidate(fact), {fact["source_ref"]: fact}, {}, [], {"c1"}, "math")
    observed = result["c1"]["claims"][0]
    assert observed["source_kind"] == "self_report"
    assert observed["help_condition"] == "unknown"


@pytest.mark.parametrize("changes,concept_id", [
    ({"explicit_student_request": False}, "c1"),
    ({"scope_kind": "course"}, "c1"),
    ({"scope_kind": "student"}, "c1"),
    ({"concept_id": "c2"}, "c1"),
])
def test_wrong_or_unauthorized_concept_correction_is_rejected(changes, concept_id):
    fact = correction(**changes)
    with pytest.raises(ValueError):
        ObservationExtractor.validate(candidate(fact, concept_id=concept_id), {fact["source_ref"]: fact}, {}, [], {"c1", "c2"}, "math")


def test_correction_cannot_be_graded_even_when_a_real_grade_is_also_referenced():
    fact = correction()
    result = candidate(fact, source_kind="graded_attempt", help_condition="unknown")
    result.observations[0].claims[0].source_refs.append("evidence_link:real:v1")
    formal = {"source_ref": "evidence_link:real:v1", "kind": "evidence_link", "payload": {
        "concept_id": "c1", "attribution": "direct", "observed_result": "correct"}}
    with pytest.raises(ValueError):
        ObservationExtractor.validate(result, {fact["source_ref"]: fact, formal["source_ref"]: formal}, {}, [], {"c1"}, "math")


def test_correction_quote_must_come_from_instruction_not_service_metadata():
    fact = correction()
    with pytest.raises(ValueError):
        ObservationExtractor.validate(candidate(fact, quote="concept"), {fact["source_ref"]: fact}, {}, [], {"c1"}, "math")


def test_concept_correction_pipeline_does_not_create_formal_state(database):
    fact = correction()
    with database() as db:
        record_fact(db, "s1", fact["source_ref"], "correction", fact["payload"])
    def extract(data):
        return {"observations": [candidate(fact).observations[0].model_dump()],
                "experience": "学生纠正先前错因描述，尚需新题核验。", "experience_source_refs": [fact["source_ref"]]}
    model = Model(extract)
    service = MemoryService(model)
    BusinessMemoryPipeline(model, service)
    snapshot = asyncio.run(service.refresh("s1"))
    assert snapshot["session_concept_observations"][0]["claims"][0]["source_kind"] == "self_report"
    with database() as db:
        assert db.scalar(select(func.count()).select_from(StudentState)) == 0


def test_aggregation_cannot_reupgrade_correction_to_formal_or_independent(database):
    fact = correction()
    with database() as db:
        record_fact(db, "s1", fact["source_ref"], "correction", fact["payload"])
    class UpgradingModel(Model):
        async def chat(self, messages, **kwargs):
            response = await super().chat(messages, **kwargs)
            if "old_claims" in json.loads(messages[1]["content"]):
                result = json.loads(response["choices"][0]["message"]["content"])
                result["claims"][0].update(source_kind="graded_attempt", help_condition="recorded_unassisted")
                response["choices"][0]["message"]["content"] = json.dumps(result, ensure_ascii=False)
            return response
    model = UpgradingModel(lambda data: {"observations": [candidate(fact).observations[0].model_dump()]})
    service = MemoryService(model)
    BusinessMemoryPipeline(model, service)
    asyncio.run(service.refresh("s1"))
    with database() as db:
        memory = db.get(ScopeMemory, scope_key("alice", "concept", "math", "c1"))
        assert memory.claims[0]["source_kind"] == "self_report"
        assert memory.claims[0]["help_condition"] == "unknown"
        assert db.scalar(select(func.count()).select_from(StudentState)) == 0
