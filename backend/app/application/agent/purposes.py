"""服务端决定上下文用途，评分输入使用白名单隔离历史标签。"""

from copy import deepcopy

PURPOSES = {"teaching", "grading", "diagnosis", "memory_extract"}
GRADING_FIELDS = {
    "task",
    "question",
    "questions",
    "student_answer",
    "deterministic_verdict",
    "target_level",
    "requested_level",
    "assisted",
    "help_snapshot",
    "submitted_response_context",
    "instruction",
    "target_concept_ids",
    "allowed_concepts",
    "assessment_criteria",
    "grading_packet",
}
HISTORY_FIELDS = {
    "delegated_materials",
    "material_transfer_receipt",
    "student_state",
    "student_profile",
    "profile",
    "prediction",
    "predictions",
    "learning_memory",
    "student_memory",
    "global_memory",
    "course_memory",
    "concept_memories",
    "session_experiences",
    "student_observations",
    "session_goals",
    "goal_state",
    "learning_progress",
    "recent_messages",
    "memories",
    "misconceptions",
}
QUESTION_FIELDS = {
    "id",
    "course_id",
    "stem",
    "stem_format",
    "images",
    "options",
    "answer",
    "explanation",
    "question_type",
    "concept_ids",
    "cognitive_type",
    "version",
    "assessment_criteria",
    "canonical_item_id",
    "blank_count",
}


def resolve_purpose(role: str, skill: str, requested: str | None) -> str:
    purpose = requested or (
        "grading"
        if role == "assessment" and skill == "diagnose"
        else "teaching"
    )
    if purpose not in PURPOSES or (purpose == "grading" and role != "assessment"):
        raise ValueError("上下文用途与执行角色不匹配")
    return purpose


def grading_payload(payload: dict) -> dict:
    """保留本题原始数据，字典中的历史画像字段不会因嵌套而重新进入模型。"""

    def clean(value, name=None):
        if isinstance(value, dict):
            if name == "question":
                # 当前答案可能是以任意空位名为键的字典，不能按历史字段名递归删答案。
                return {
                    key: deepcopy(item) for key, item in value.items() if key in QUESTION_FIELDS
                }
            return {
                key: clean(item, key) for key, item in value.items() if key not in HISTORY_FIELDS
            }
        if isinstance(value, list):
            return [clean(item) for item in value]
        return deepcopy(value)

    return {key: clean(value, key) for key, value in payload.items() if key in GRADING_FIELDS}
