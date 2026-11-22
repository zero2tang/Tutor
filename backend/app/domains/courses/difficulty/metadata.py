"""每题唯一难度估计；历史口径可追溯，题面改变立即失效。"""

from hashlib import sha256
import json
import math


def fingerprint(question):
    validation = question.validation or {}
    value = {
        key: getattr(question, key)
        for key in (
            "id",
            "course_id",
            "stem",
            "options",
            "answer",
            "explanation",
            "concept_ids",
            "version",
        )
    }
    value["images"] = validation.get("images", [])
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def estimate(question):
    value = dict(getattr(question, "difficulty_estimate", None) or {})
    if not value:
        return {"value": None, "status": "unknown"}
    if value.get("question_fingerprint") != fingerprint(question):
        return {**value, "value": None, "status": "stale"}
    score = value.get("value")
    if (
        value.get("status") != "estimated"
        or not isinstance(score, (int, float))
        or isinstance(score, bool)
        or not math.isfinite(score)
        or value.get("scale_max") != 100
        or not 0 <= score <= 100
        or not all(
            value.get(key)
            for key in ("method", "version", "reference_group", "source_hash")
        )
        or value.get("higher_is_harder") is not True
    ):
        value["value"] = None
    return value


def public_estimate(question):
    value = estimate(question)
    fields = (
        "value",
        "status",
        "method",
        "version",
        "reference_group",
        "scale_max",
        "higher_is_harder",
        "model",
        "confidence",
        "source_hash",
    )
    return {key: value.get(key) for key in fields if key in value}
