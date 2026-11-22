"""题目附属教学参考及旧目录的短期投影；历史原包只从只读归档恢复。"""

from copy import deepcopy
from types import SimpleNamespace
from datetime import datetime
from typing import Literal
from functools import lru_cache
from hashlib import sha256
import gzip
import json
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from app.domains.courses.models import Question


class TeachingReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = 1
    legacy_id: str
    legacy_hash: str
    document_id: str
    source_question_id: str
    status: str
    created_at: str
    catalog: dict
    problem_metadata: dict
    answer_metadata: dict
    answer_shape: Literal["none", "scalar", "container"]
    source_solution_metadata: list[dict] = Field(default_factory=list)
    reference_hash: str = ""
    archived: bool = False


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def make_reference(pack, *, archived=False):
    payload = deepcopy(pack.payload)
    problem = payload.pop("problem")
    problem.pop("stem", None)
    answer = payload.pop("source_answer", {})
    value = answer.pop("value", None)
    solutions = payload.pop("source_solution", [])
    for item in solutions:
        item["text_length"] = len(item.pop("text", ""))
    ref = TeachingReference(legacy_id=pack.id, legacy_hash=pack.payload_hash,
        document_id=pack.document_id, source_question_id=pack.source_question_id,
        status=pack.status, created_at=pack.created_at.isoformat(), catalog=payload,
        problem_metadata=problem, answer_metadata=answer, source_solution_metadata=solutions,
        answer_shape="none" if value is None else "scalar" if not isinstance(value, (list, dict)) else "container",
        archived=archived).model_dump()
    ref["reference_hash"] = digest({k:v for k,v in ref.items() if k != "reference_hash"})
    return ref


@lru_cache(maxsize=1)
def archives():
    path = Path(__file__).resolve().parents[4] / "data/archives/simplification_p1/20261023/teaching_packs.json.gz"
    return json.loads(gzip.decompress(path.read_bytes()))


def view(question):
    raw = question.teaching_reference or {}
    ref = TeachingReference.model_validate(raw)
    if digest({k:v for k,v in raw.items() if k != "reference_hash"}) != ref.reference_hash:
        raise ValueError("教学参考指纹不一致")
    if ref.archived:
        payload = deepcopy(archives()[ref.legacy_id])
    else:
        payload = deepcopy(ref.catalog)
        payload["problem"] = {**ref.problem_metadata, "stem": question.stem}
        value = None if ref.answer_shape == "none" else question.answer[0] if ref.answer_shape == "scalar" else question.answer
        payload["source_answer"] = {**ref.answer_metadata, "value": deepcopy(value)}
        payload["source_solution"] = []
        offset = 0
        for metadata in ref.source_solution_metadata:
            item = deepcopy(metadata)
            length = item.pop("text_length")
            item["text"] = question.explanation[offset:offset+length]
            offset += length + 1
            payload["source_solution"].append(item)
    if digest(payload) != ref.legacy_hash:
        raise ValueError("教学原档案指纹不一致")
    return SimpleNamespace(id=ref.legacy_id, course_id=question.course_id, document_id=ref.document_id,
        question_id=question.id, source_question_id=ref.source_question_id, status=ref.status,
        payload_hash=ref.legacy_hash, payload=payload, created_at=datetime.fromisoformat(ref.created_at))


def pack_views(db, course_id=None, document_id=None):
    stmt = select(Question.id).where(Question.teaching_reference["legacy_id"].as_string().is_not(None))
    if course_id:
        stmt = stmt.where(Question.course_id == course_id)
    if document_id:
        stmt = stmt.where(Question.teaching_reference["document_id"].as_string() == document_id)
    return [view(db.get(Question, identifier)) for identifier in db.scalars(stmt)]


def get_pack(db, identifier):
    if not identifier:
        return None
    q = db.scalar(select(Question).where(Question.teaching_reference["legacy_id"].as_string() == identifier))
    return view(q) if q else None


def source_question_map(db, course_id, document_id):
    return {source: identifier for identifier, source in db.execute(select(
        Question.id, Question.teaching_reference["source_question_id"].as_string()).where(
        Question.course_id == course_id,
        Question.teaching_reference["document_id"].as_string() == document_id))}
