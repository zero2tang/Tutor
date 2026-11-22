"""独立作答期间的公开记忆投影，不改变后台教师上下文和持久化记录。"""

from copy import deepcopy


def public_memory_snapshot(snapshot: dict, session) -> dict:
    context = session.pending_context or {}
    guidance = context.get("guided_teaching") or {}
    transfer = guidance.get("transfer") or {}
    batch = context.get("exercise_batch") or {}
    restricted = bool(
        (session.pending_question_id and not context.get("assisted", False))
        or any(not item.get("assisted", False) for item in batch.get("items", []))
        or (transfer and not transfer.get("assisted", False) and not guidance.get("closed", False))
    )
    result = deepcopy(snapshot)
    result["presentation_restricted"] = restricted
    if restricted:
        def redact(value):
            if isinstance(value, dict):
                return {**value, "content": "", "claims": [], "source_refs": []}
            return value
        for field in ("global_memory", "course_memory", "student_memory", "session_experience"):
            if field in result:
                result[field] = redact(result[field])
        for field in ("concept_memories", "concept_observations", "session_concept_observations", "session_experiences"):
            if field in result:
                result[field] = [redact(item) for item in result[field]]
        result.pop("unassigned", None)
    return result
