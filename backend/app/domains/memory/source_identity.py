"""只按可信题目身份统计独立任务；会话、作答及工作区UUID均不代表新题。"""

from hashlib import sha256
import json
import re
import unicodedata


def _key(course_id, category, identity):
    value = json.dumps([course_id, category, identity], ensure_ascii=False, sort_keys=True)
    return category + ":" + sha256(value.encode()).hexdigest()


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _content_key(problem, course_id):
    if not isinstance(problem, dict) or not isinstance(problem.get("stem"), str) or not problem["stem"].strip():
        return None

    def clean(value):
        if isinstance(value, str):
            return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))
        if isinstance(value, dict):
            return {str(key): clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    # 与guidance.question_hash共享文本规范化；解析、版本、ID变化不增加任务。
    text = clean(problem["stem"])
    if problem.get("options") or problem.get("images"):
        text += json.dumps(clean({key: problem.get(key) for key in ("options", "images")}),
                           ensure_ascii=False, sort_keys=True)
    content_hash = sha256(text.encode()).hexdigest()
    return _key(course_id, "content", content_hash)


def source_task_id(payload: dict, course_id: str = "", *, catalog_question=None,
                   source_kind: str | None = None) -> str | None:
    """来源必须是服务保存的结构；未确认目录身份的question_id不作为准入依据。"""
    payload = _mapping(payload)
    assessment = _mapping(payload.get("assessment"))
    packet = _mapping(assessment.get("grading_packet"))
    for value in (payload.get("canonical_item_id"), packet.get("canonical_item_id")):
        if isinstance(value, str) and value.strip():
            return _key(course_id, "canonical", value.strip())
    details = _mapping(payload.get("details"))
    for value in (payload.get("question_hash"), details.get("question_hash")):
        if isinstance(value, str) and re.fullmatch(r"[a-fA-F0-9]{64}", value):
            return _key(course_id, "content", value.lower())
    metadata = _mapping(payload.get("metadata"))
    snapshot = _mapping(metadata.get("guidance_snapshot"))
    active = snapshot.get("transfer") or snapshot.get("problem")
    candidates = [active, payload.get("problem"), payload.get("question_contract"), payload.get("contract"),
                  details.get("problem"), details.get("question_contract"), assessment.get("question_contract")]
    if source_kind != "message":
        candidates.append(payload)  # 原始聊天正文不能被当成冻结题干。
    for problem in candidates:
        key = _content_key(problem, course_id)
        if key:
            return key
    if catalog_question is not None:
        catalog = catalog_question if isinstance(catalog_question, dict) else {
            key: getattr(catalog_question, key, None) for key in ("id", "course_id", "stem", "options")}
        if catalog.get("course_id") == course_id:
            key = _content_key(catalog, course_id)
            if key:
                return key
            if catalog.get("id"):
                return _key(course_id, "catalog", str(catalog["id"]))
    # 未知任务可以进入描述性记忆，不能替课程或总体模式凑足独立任务数量。
    return None
