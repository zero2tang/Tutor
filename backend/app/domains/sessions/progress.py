"""题目切换时保留已提交的学习流程状态，不产生掌握度证据。"""

from copy import deepcopy

PERSISTENT_CONTEXT_KEYS = ("learning_progress",)


def preserve_learning_context(context: dict | None) -> dict:
    """题目状态切换时只保留正式学习进度，不延续旧题契约或待澄清决策。"""
    source = context or {}
    return {
        key: deepcopy(source[key]) for key in PERSISTENT_CONTEXT_KEYS if key in source
    }
