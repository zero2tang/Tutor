"""未冻结时只报告真实完成度与缺口，不发布临时质量分数。"""

from pathlib import Path
from collections import Counter
from .files import read_rows, read_json, write_json, atomic_text
from .config import HOME
from .dataset import validate_dataset
from .costs import summarize_costs


def status_report(dataset):
    from .reporting import page
    import html
    import json

    dataset = Path(dataset)
    corpus = read_rows(dataset / "corpus.jsonl")
    qrels = read_rows(dataset / "qrels.jsonl")
    pools = read_rows(dataset / "pools.jsonl")
    result = {
        "dataset": dataset.name,
        "frozen": (dataset / "freeze.json").exists(),
        "corpus": len(corpus),
        "transcriptions": dict(Counter(r["transcription_status"] for r in corpus)),
        "index": read_json(dataset / "index.json", {}).get("status", "not_started"),
        "pooled_queries": len(pools),
        "pool_pairs": sum(len(p["candidate_ids"]) for p in pools),
        "labeled_pairs": len(qrels),
        "review_completed_pairs": sum(r.get("review_complete", False) for r in qrels),
        "unknown_pairs": sum(r["grade"] is None for r in qrels),
        "validation": validate_dataset(dataset, require_labels=True),
        "cost": summarize_costs(dataset),
        "notice": "准备状态，不是正式检索质量评分；历史失败回执保留在work目录。",
    }
    prefix = HOME / "reports" / (dataset.name + "_readiness")
    write_json(prefix.with_suffix(".json"), result)
    atomic_text(
        prefix.with_suffix(".html"),
        page(
            "习题检索评测准备状态",
            "<pre>"
            + html.escape(json.dumps(result, ensure_ascii=False, indent=2))
            + "</pre>",
        ),
    )
    return result
