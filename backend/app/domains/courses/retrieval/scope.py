"""材料和题目检索共享范围表达；调用方负责提供可信课程及排除集合。"""

from app.domains.courses.questions.policy import field


def filters(course_id, concepts, coverage="any", question_type=None, universe=None):
    result = [{"term": {"course_id": course_id}}, {"term": {"visibility": "public"}}]
    if universe is not None:
        result.append({"bool": {"should": [{"terms": {"question_id": sorted(universe)}}, {"ids": {"values": sorted(universe)}}], "minimum_should_match": 1}})
    if concepts:
        result.extend([{"term": {"concept_ids": c}} for c in concepts] if coverage == "all" else [{"terms": {"concept_ids": list(concepts)}}])
    if question_type:
        result.append({"term": {"question_type": question_type}})
    return result


def search_scope(course_id, concepts=(), coverage="any", question_type=None, exclusions=(), *, questions=False, universe=None):
    denied = [{"terms": {"question_id": list(exclusions)}}] if exclusions else []
    if questions and exclusions:
        denied.append({"ids": {"values": list(exclusions)}})
    return {"bool": {"filter": filters(course_id, concepts, coverage, question_type, universe), "must_not": denied}}


def matches_scope(question, *, concept_ids=(), coverage="any", question_type=None, exclusions=(), universe=None):
    identifier = field(question, "id")
    if identifier in exclusions or (universe is not None and identifier not in universe):
        return False
    if question_type and field(question, "question_type") != question_type:
        return False
    actual = field(question, "concept_ids", []) or []
    return not concept_ids or (all(c in actual for c in concept_ids) if coverage == "all" else any(c in actual for c in concept_ids))
