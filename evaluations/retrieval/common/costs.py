"""汇总实际回执，不用缓存调用数量冒充远程请求，也不估算未知单价。"""

from collections import defaultdict
from pathlib import Path
from .files import read_rows


def summarize_costs(dataset):
    groups = defaultdict(
        lambda: {
            "operations": 0,
            "http_attempts_recorded": 0,
            "cache_hits": 0,
            "failed_operations": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "elapsed_ms": 0.0,
            "usage_missing_operations": 0,
        }
    )

    def add(group, receipt, failed=False):
        value = groups[group]
        value["operations"] += 1
        value["failed_operations"] += int(failed)
        value["cache_hits"] += int(receipt.get("cache_hit", False))
        request = receipt.get("request_metrics", {})
        value["elapsed_ms"] += (
            receipt.get("elapsed_ms", request.get("duration_ms", 0)) or 0
        )
        value["http_attempts_recorded"] += (
            request.get("request_attempts", receipt.get("requests", 0)) or 0
        )
        usage = receipt.get("usage", {})
        if not usage and not receipt.get("cache_hit"):
            value["usage_missing_operations"] += 1
        value["prompt_tokens"] += (
            usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
        )
        value["completion_tokens"] += (
            usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
        )
        value["total_tokens"] += (
            usage.get(
                "total_tokens",
                (usage.get("input_tokens", 0) or 0)
                + (usage.get("output_tokens", 0) or 0),
            )
            or 0
        )

    dataset = Path(dataset)
    for directory, prefix in (
        (dataset / "work", ""),
        (dataset / "work/inherited", "inherited_parent/"),
    ):
        for row in read_rows(directory / "usage.jsonl"):
            add(prefix + row["role"], row["metrics"], row.get("status") == "failed")
        for row in read_rows(directory / "encoding_usage.jsonl"):
            for encoder, receipt in row["usage"].items():
                group = prefix + row["phase"] + "/" + encoder
                if "batches" in receipt:
                    for batch in receipt["batches"]:
                        add(group, batch)
                    groups[group]["cache_hits"] += receipt.get("cache_hits", 0)
                else:
                    add(group, receipt)
    for value in groups.values():
        value["failure_rate"] = (
            value["failed_operations"] / value["operations"]
            if value["operations"]
            else None
        )
    return {
        "groups": dict(groups),
        "currency_cost": None,
        "notice": "仅汇总服务商回执；无回执的失败无法确认计费Tokens。缓存命中含本地模型缓存；未配置单价，不虚构金额。",
        "inherited_notice": "inherited_parent为父版本60查询及替换查询的全部准备开销，已发生且供本版本复用；与24查询新增开销分别列出，不能算成24查询独占成本。",
        "receipts": [
            str(dataset / "work/usage.jsonl"),
            str(dataset / "work/encoding_usage.jsonl"),
            str(dataset / "work/inherited/usage.jsonl"),
            str(dataset / "work/inherited/encoding_usage.jsonl"),
        ],
    }
