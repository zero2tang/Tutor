"""课程资料的有界词面候选检索。"""

import re

from sqlalchemy import Text, cast, or_, select
from app.domains.courses.models import Concept, Course, Material, Question
from app.domains.materials.teaching_reference import get_pack

from app.domains.materials.resources.projections import BLOCK_KINDS, resource_summary


def _terms(query):
    values = [query.strip()]
    values.extend(re.findall(r"[A-Za-z][A-Za-z0-9_]{1,}|[\u3400-\u9fff]{2,}", query))
    for text in re.findall(r"[\u3400-\u9fff]{3,}", query):
        values.extend(text[i : i + 2] for i in range(len(text) - 1))
    return list(dict.fromkeys(value.casefold() for value in values if value))[:20]


def _score(text, terms):
    text = text.casefold()
    return sum((len(term) + 1) for term in terms if term in text) if terms else 1


def _where(fields, terms):
    return or_(
        *(field.icontains(term, autoescape=True) for field in fields for term in terms)
    )


def search_resources(
    access,
    database,
    course_id,
    query,
    kind="all",
    limit=6,
    *,
    excluded_question_ids=(),
    session_id=None,
    student_id="local",
):
    if (
        kind not in {"all", "examples", "concepts", "strategies"}
        or not isinstance(query, str)
        or len(query) > 1000
    ):
        raise ValueError("教学资料类型或查询长度无效。")
    if type(limit) is not int or not 1 <= limit <= 8:
        raise ValueError("教学资料每次返回 1 至 8 条。")
    terms, candidates = _terms(query), []
    with database() as db:
        excluded = access.scope(
            db, course_id, excluded_question_ids, session_id, student_id
        )
        concept_query = select(Concept).where(
            Concept.course_id == course_id, Concept.topic_type != "category"
        )
        if terms:
            concept_query = concept_query.where(
                _where((Concept.name, Concept.summary), terms)
            )
        concepts = list(db.scalars(concept_query.order_by(Concept.id).limit(80)))
        course = db.get(Course, course_id)
        if kind in {"all", "concepts"}:
            for concept in concepts:
                item = resource_summary(
                    f"teach:concept:{concept.id}",
                    "concept",
                    concept.name,
                    concept.summary,
                    course_id,
                    course.source_ref,
                    origin="course_catalog",
                )
                candidates.append((_score(concept.name + concept.summary, terms), item))
            material_query = select(Material).where(
                Material.course_id == course_id,
                Material.visibility == "public",
                Material.question_id.is_(None),
            )
            if terms:
                material_query = material_query.where(
                    _where((Material.title, Material.text), terms)
                )
            for material in db.scalars(material_query.order_by(Material.id).limit(80)):
                item = resource_summary(
                    f"teach:material:{material.id}",
                    "concept",
                    material.title,
                    material.text,
                    course_id,
                    material.source_ref,
                    origin=material.origin,
                )
                candidates.append((_score(material.title + material.text, terms), item))
        if kind in {"all", "examples"}:
            statement = select(Question).where(Question.course_id == course_id)
            if excluded:
                statement = statement.where(Question.id.not_in(excluded))
            if terms:
                matches = [_where((Question.stem, Question.id), terms)]
                matches.extend(
                    cast(Question.concept_ids, Text).contains(
                        '"' + concept.id + '"', autoescape=True
                    )
                    for concept in concepts
                )
                statement = statement.where(or_(*matches))
            for question in db.scalars(statement.order_by(Question.id).limit(100)):
                if (question.validation or {}).get("availability", "ready") != "ready":
                    continue
                pack = (
                    get_pack(db, (question.validation or {}).get("teaching_pack_id"))
                    if (question.validation or {}).get("teaching_pack_id")
                    else None
                )
                refs = []
                if pack:
                    try:
                        document = access.archive(db, pack, course_id, excluded)
                    except (ValueError, PermissionError):
                        continue
                    refs = access.refs(
                        document,
                        [
                            ref["block_id"]
                            for ref in pack.payload.get("source_refs", [])
                            if ref.get("block_id")
                        ],
                    )
                item = resource_summary(
                    f"teach:question:{question.id}",
                    "example",
                    question.stem[:120],
                    question.stem,
                    course_id,
                    question.source_ref,
                    refs,
                    "model_generated" if question.generated else "source_exercise",
                )
                item["concept_ids"] = list(question.concept_ids)
                item["candidate_notice"] = (
                    "仅为检索候选，由模型比较结构、方法与条件；未认定先修关系。"
                )
                candidates.append(
                    (
                        _score(question.stem, terms)
                        + len(
                            set(question.concept_ids)
                            & {concept.id for concept in concepts}
                        ),
                        item,
                    )
                )
        if kind in {"all", "concepts", "strategies"}:
            for document in access.documents(db, course_id):
                private = access.private_block_ids(document)
                for block in document.parsed.get("blocks", []):
                    block_kind = block.get("kind")
                    if block_kind not in BLOCK_KINDS or block["id"] in private:
                        continue
                    is_strategy = block_kind == "technique"
                    if (
                        kind == "strategies"
                        and not is_strategy
                        or kind == "concepts"
                        and is_strategy
                    ):
                        continue
                    score = _score(block.get("text", ""), terms)
                    if score:
                        item = resource_summary(
                            f"teach:block:{document.id}:{block['id']}",
                            "strategy" if is_strategy else "concept",
                            document.title + " · " + block_kind,
                            block.get("text", ""),
                            course_id,
                            block.get("source", {}).get("source_uri")
                            or document.source_uri,
                            access.refs(document, [block["id"]]),
                            "textbook_explicit",
                        )
                        candidates.append((score, item))
        if kind in {"all", "strategies"}:
            for pack in access.packs(db, course_id):
                try:
                    document = access.archive(db, pack, course_id, excluded)
                except (ValueError, PermissionError):
                    continue
                for strategy in pack.payload.get("techniques", []):
                    text = strategy.get("name", "") + " " + strategy.get("basis", "")
                    score = _score(text, terms)
                    if score:
                        refs = access.refs(
                            document,
                            [
                                ref["block_id"]
                                for ref in strategy.get("source_refs", [])
                                if ref.get("block_id")
                            ],
                        )
                        item = resource_summary(
                            f"teach:strategy:{pack.id}:{strategy['id']}",
                            "strategy",
                            strategy.get("name", strategy["id"]),
                            strategy.get("basis", ""),
                            course_id,
                            document.source_uri,
                            refs,
                            strategy.get("provenance", {}).get(
                                "origin", "archived_analysis"
                            ),
                        )
                        candidates.append((score, item))
    candidates.sort(key=lambda pair: (-pair[0], pair[1]["resource_id"]))
    unique = {item["resource_id"]: item for score, item in candidates if score > 0}
    items = list(unique.values())[:limit]
    return {
        "items": items,
        "citations": [item["citation"] for item in items],
        "returned_count": len(items),
        "channels_used": ["course_database_and_textbook_rules"],
        "matching": "bounded_lexical_candidates",
        "notice": "没有命中不表示无法讲解；这些候选并未被认定为相似、先修或必要解法。",
    }
