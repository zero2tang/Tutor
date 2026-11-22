from app.domains.memory.models import MemoryJob
from app.domains.memory.scopes.models import SessionMemoryExtraction
from reference_helpers import put_scope
"""同题重建、版本和会话UUID不能替上层模式增加独立任务。"""

from hashlib import sha256
import re
import unicodedata

from app.domains.memory.scopes.promotion import promotion_eligible
from app.domains.memory.source_identity import source_task_id


def question_hash(stem):
    return sha256(re.sub(r"\s+", "", unicodedata.normalize("NFKC", stem)).encode()).hexdigest()


def test_canonical_task_precedes_content_hash_and_random_problem_id():
    payload = {"canonical_item_id": "canonical-a", "question_id": "random-problem-1", "question_hash": "a" * 64}
    expected = source_task_id(payload, "math")
    assert expected == source_task_id({**payload, "question_id": "random-problem-2", "question_hash": "b" * 64}, "math")
    assert expected != source_task_id({**payload, "canonical_item_id": "canonical-b"}, "math")
    nested = {"assessment": {"grading_packet": {"canonical_item_id": "canonical-a"}}, "question_hash": "c" * 64}
    assert source_task_id(nested, "math") == expected


def test_same_guidance_question_hash_ignores_new_session_and_problem_uuid():
    stem = "4(x-2)=12"
    first = {"question_id": "problem-uuid-1", "question_hash": question_hash(stem), "session_id": "session-1"}
    second = {**first, "question_id": "problem-uuid-2", "session_id": "session-2"}
    assert source_task_id(first, "math") == source_task_id(second, "math")
    assert source_task_id(first, "math") == source_task_id({"details": {"question_hash": question_hash(stem)}}, "math")


def test_top_level_content_hash_precedes_details_hash():
    assert source_task_id({"question_hash": "a" * 64, "details": {"question_hash": "b" * 64}}, "math") == source_task_id(
        {"question_hash": "a" * 64}, "math")


def test_frozen_problem_normalizes_layout_and_reuses_same_content_hash():
    first = source_task_id({"problem": {"id": "p1", "stem": "４ (x - 2) = 12", "version": 1}}, "math")
    second = source_task_id({"problem": {"id": "p2", "stem": "4(x-2)=12", "version": 9}}, "math")
    assert first == second
    assert first == source_task_id({"question_hash": question_hash("4(x-2)=12")}, "math")
    assert first != source_task_id({"problem": {"stem": "4(x-2)=12"}}, "physics")


def test_chat_reply_and_random_activity_id_are_unknown_tasks():
    assert source_task_id({"role": "user", "content": "我会了", "session_id": "s1",
                           "question_id": "random-problem", "activity_id": "a1"}, "math", source_kind="message") is None
    assert source_task_id({"stem": "学生自由文本不是冻结题干"}, "math", source_kind="message") is None
    assert source_task_id({"question_hash": "not-a-content-hash"}, "math") is None


def test_service_verified_catalog_question_uses_content_and_checks_course():
    payload = {"question_id": "catalog-1"}
    catalog = {"id": "catalog-1", "course_id": "math", "stem": "4(x-2)=12"}
    assert source_task_id(payload, "math") is None
    assert source_task_id(payload, "math", catalog_question=catalog) == source_task_id(
        {"question_hash": question_hash(catalog["stem"])}, "math")
    assert source_task_id(payload, "physics", catalog_question=catalog) is None


def test_assistant_frozen_snapshot_uses_active_problem_not_message_id():
    payload = {"role": "assistant", "metadata": {"guidance_snapshot": {"problem": {"id": "random-1", "stem": "4(x-2)=12"}}}}
    expected = source_task_id({"question_hash": question_hash("4(x-2)=12")}, "math")
    assert source_task_id(payload, "math", source_kind="message") == expected


def test_unknown_or_one_content_task_cannot_satisfy_promotion_gate():
    facts = {"a": {"task_id": None, "course_id": "math", "concept_ids": ["c1"]},
             "b": {"task_id": None, "course_id": "math", "concept_ids": ["c2"]}}
    claim = {"source_refs": ["a", "b"]}
    assert not promotion_eligible(claim, "course", facts, [])
    task = source_task_id({"question_hash": "a" * 64}, "math")
    facts["a"]["task_id"] = facts["b"]["task_id"] = task
    assert not promotion_eligible(claim, "course", facts, [])
    facts["b"]["task_id"] = source_task_id({"question_hash": "b" * 64}, "math")
    assert promotion_eligible(claim, "course", facts, [])


def test_unknown_task_cannot_supply_extra_course_or_concept_for_gate():
    facts = {"a": {"task_id": "known-1", "course_id": "math", "concept_ids": ["c1"]},
             "b": {"task_id": "known-2", "course_id": "math", "concept_ids": ["c1"]},
             "c": {"task_id": None, "course_id": "physics", "concept_ids": ["c2"]}}
    claim = {"source_refs": list(facts)}
    assert not promotion_eligible(claim, "student", facts, [])
    assert not promotion_eligible(claim, "course", facts, [])
