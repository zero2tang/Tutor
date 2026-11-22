"""记忆提取、失败修复与任务重试共用的调用开销累计。"""

def merge_metrics(old: dict, latest: dict | None) -> dict:
    """失败修复和任务重试的模型开销也计入，服务商未返回 usage 时明确标缺失。"""
    if latest is None:
        return old
    result = dict(old or {})
    for key in ("model_calls", "prompt_tokens", "completion_tokens", "duration_ms", "usage_unavailable_calls"):
        result[key] = round(result.get(key, 0) + latest.get(key, 0), 2)
    return result
