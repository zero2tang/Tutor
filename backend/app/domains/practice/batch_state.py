"""练习组状态的公开投影，统一隔离内部答案与浏览器数据。"""

from app.domains.practice.context import public_exercise


def public_batch(batch: dict, course_id: str) -> dict:
    questions = []
    for item in batch["items"]:
        question = public_exercise(item["contract"], course_id)
        question["practice_type"] = item["cognitive_type"]
        question["exercise_id"] = item["exercise_id"]
        questions.append(question)
    return {
        "id": batch["id"],
        "title": batch["title"],
        "questions": questions,
        "assisted_question_ids": [
            item["contract"]["id"] for item in batch["items"] if item.get("assisted")
        ],
    }


def mark_batch_assisted(pending: dict, question_id: str | None = None) -> dict:
    """更新复制后的 JSON，题解仅标记对应题，知识讲解保守标记整组。"""
    from copy import deepcopy

    result = deepcopy(pending)
    batch = result.get("exercise_batch")
    if batch:
        for item in batch["items"]:
            if question_id is None or item["contract"]["id"] == question_id:
                item["assisted"] = True
    else:
        result["assisted"] = True
    return result
