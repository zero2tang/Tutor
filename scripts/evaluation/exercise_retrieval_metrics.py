"""意图相关的逐题指标；来源组划分与标签先于最终排序结果确定。"""

import math
import statistics


def query_metrics(ids, labels, k=5):
    grades = [labels.get(qid, 0) for qid in ids[:k]]
    relevant = {qid for qid, grade in labels.items() if grade >= 2}
    recalled = sum(qid in relevant for qid in ids[:k])
    reciprocal = next((1 / (i + 1) for i, qid in enumerate(ids[:k]) if qid in relevant), 0)
    def dcg(values):
        return sum((2 ** grade - 1) / math.log2(i + 2) for i, grade in enumerate(values))
    ideal = dcg(sorted(labels.values(), reverse=True)[:k])
    return {"recall_at_k": recalled / len(relevant) if relevant else None,
        "mrr": reciprocal, "top1": int(bool(ids) and ids[0] in relevant), "ndcg_at_k": dcg(grades) / ideal if ideal else None}


def summarize(rows):
    values = {}
    for row in rows:
        group = row["group"]
        metrics = row["metrics"]
        values.setdefault(group, []).append((metrics, row))
    results = {}
    for group, entries in values.items():
        durations = sorted(r["result"]["timings"]["total_ms"] for _, r in entries)
        def average(key):
            samples = [m[key] for m, _ in entries if m[key] is not None]
            return statistics.mean(samples) if samples else None
        results[group] = {"queries": len(entries), **{key: average(key) for key in ("recall_at_k", "mrr", "top1", "ndcg_at_k")},
            "p50_ms": statistics.median(durations), "p95_ms": durations[min(len(durations) - 1, math.ceil(len(durations) * .95) - 1)],
            "degraded": sum(r["result"]["degraded"] for _, r in entries)}
    return results


def usage_totals(value):
    """只累计真实回执计数，不将缓存命中当成新请求，也不把总Tokens与分项重复相加。"""
    total = {"request_attempts": 0, "prompt_or_input_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    def visit(node):
        if isinstance(node, dict):
            metrics = node.get("request_metrics")
            if isinstance(metrics, dict) and isinstance(metrics.get("request_attempts"), int):
                total["request_attempts"] += metrics["request_attempts"]
            usage = node.get("usage")
            if isinstance(usage, dict) and any(k in usage for k in ("prompt_tokens", "input_tokens", "completion_tokens", "total_tokens")):
                total["prompt_or_input_tokens"] += usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
                total["completion_tokens"] += usage.get("completion_tokens", 0) or 0
                total["total_tokens"] += usage.get("total_tokens", 0) or 0
            for key, child in node.items():
                if key not in {"request_metrics", "usage"}:
                    visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)
    visit(value)
    return total
