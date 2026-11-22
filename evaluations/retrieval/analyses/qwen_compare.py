"""在同一份已标注前缀上比较两个本地 Qwen 精排模型。"""

from ..common.config import HOME
from ..common.files import read_json, write_json
from .qwen_replay import compare


REPORTS = HOME / "reports"
PREFIX = "math_100_q24_v1_qwen_"


def main():
    reports = {kind: read_json(REPORTS / f"{PREFIX}{kind}.json") for kind in ("text", "vl")}
    text, vl = reports["text"], reports["vl"]
    if text is None or vl is None or text["queries"] != vl["queries"] or text["queries"] != 24:
        raise ValueError("需要两个模型的完整实测报告")
    for field in ("dataset_hash", "candidate_source_sha256", "candidate_count"):
        if text[field] != vl[field]:
            raise ValueError(f"Qwen 实验来源不一致：{field}")
    rows = []
    for a, b in zip(text["per_query"], vl["per_query"]):
        if any(a[k] != b[k] for k in ("query_id", "candidates", "input_kind", "candidate_recall")):
            raise ValueError("两个模型的候选不一致")
        rows.append({**a, "metrics": {**a["metrics"], "vl": b["metrics"]["vl"]}})
    result = {
        "dataset_hash": text["dataset_hash"], "candidate_source_sha256": text["candidate_source_sha256"],
        "queries": len(rows), "candidate_count": text["candidate_count"],
        "summary": compare(rows, ("vector", "ik_rrf", "shortlist_rrf", "text", "vl")),
        "runtime": {kind: {"model": r["model"], "scored_pairs": r["scored_pairs"],
                           "batch_size": r["batch_size"], **r["timing"]} for kind, r in reports.items()},
        "unjudged_at_5": {row["query_id"]: {kind: row["metrics"][kind]["5"]["unjudged"] for kind in reports}
                          for row in rows if any(row["metrics"][k]["5"]["unjudged"] for k in reports)},
        "sum_case_math1049": {a["query_id"]: {kind: r["rank_of_positive"]["math:1049"]
                                            for kind, r in (("text", a), ("vl", b))}
                             for a, b in zip(text["per_query"], vl["per_query"])
                             if a["source_id"] == "math:1587"},
    }
    target = REPORTS / f"{PREFIX}comparison.json"
    write_json(target, result)
    print(target)


if __name__ == "__main__":
    main()
