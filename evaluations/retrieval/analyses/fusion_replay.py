"""复用冻结的向量与 IK 候选，比较两条简单的同解法排序规则。"""

from collections import defaultdict

from app.domains.courses.retrieval.fusion import fuse

from ..common.config import DEFAULT_DATASET, HOME
from ..common.files import file_hash, read_json, read_rows, verify_freeze, write_json
from ..common.metrics import aggregate, query_metrics


SOURCE = HOME / "reports/math_100_q24_v1_ik_replay.json"
TARGET = HOME / "reports/math_100_q24_v1_fusion_replay.json"
VARIANTS = ("vector", "ik_rrf60", "same_method_rrf10", "same_method_vector_top3")
METRICS = ("recall_at_10", "ndcg_at_10", "hit_at_10", "mrr_at_10")


def compare():
    frozen = verify_freeze(DEFAULT_DATASET)
    previous = read_json(SOURCE)
    if previous["dataset_hash"] != frozen["dataset_hash"]:
        raise ValueError("候选排名与冻结数据不匹配")
    queries = {row["id"]: row for row in read_rows(DEFAULT_DATASET / "queries.jsonl")}
    previous_queries = {row["query_id"]: row for row in previous["per_query"]}
    if set(queries) != set(previous_queries) or len(queries) != 24:
        raise ValueError("回放必须包含全部 24 条冻结查询")
    labels = defaultdict(dict)
    for row in read_rows(DEFAULT_DATASET / "qrels.jsonl"):
        labels[row["query_id"]][row["question_id"]] = row["grade"]

    rows = []
    for qid, q in queries.items():
        prior = previous_queries[qid]
        vector = prior["rankings"]["E1"]
        keyword = [hit["id"] for hit in prior["keyword_rankings"]["IK"]]
        # RRF 只使用每路名次，复用业务的融合函数，无需重算原始分数。
        channels = {name: [{"_id": cid} for cid in ids]
                    for name, ids in (("keyword", keyword), ("text_vector", vector))}
        rrf60 = [hit["id"] for hit in fuse(channels, k=60)]
        if rrf60 != prior["rankings"]["E3_IK"]:
            raise ValueError(f"不能重现当前 IK 融合排名：{qid}")
        same_method = q["intent"] == "same_method"
        head = vector[:3]
        rrf10 = [hit["id"] for hit in fuse(channels, k=10)] if same_method else rrf60
        top3 = head + [cid for cid in rrf60 if cid not in head] if same_method else rrf60
        rankings = dict(zip(VARIANTS, (vector, rrf60, rrf10, top3), strict=True))
        if any(len(ids) != len(set(ids)) or set(ids) != set(rrf60) for ids in (rrf10, top3)):
            raise ValueError(f"排序对照改变了候选集合：{qid}")
        judgments = labels[qid]
        positives = {cid for cid, grade in judgments.items() if grade is not None and grade >= 2}
        rows.append({
            "query_id": qid, "intent": q["intent"], "split": q["split"],
            "source_id": q["source_id"], "source_group": q["source_group"], "query": q["text"],
            "rankings": rankings,
            "unjudged_at_10": {v: [cid for cid in ids[:10] if judgments.get(cid) is None]
                              for v, ids in rankings.items()},
            "metrics": {v: query_metrics(ids[:10], judgments) for v, ids in rankings.items()},
            "positive_ranks": {cid: {v: ids.index(cid) + 1 if cid in ids else None
                                     for v, ids in rankings.items()} for cid in sorted(positives)},
            "keyword_only_positives_in_top10": {
                v: sorted(set(ids[:10]) & (positives - set(vector))) for v, ids in rankings.items()
            },
        })

    summary = {}
    for split in ("development", "test"):
        summary[split] = {}
        for intent in ("all", "exact", "isomorphic", "same_method"):
            selected = [r for r in rows if r["split"] == split and (intent == "all" or r["intent"] == intent)]
            paired = [r for r in selected if all(r["metrics"][v]["label_complete"] for v in VARIANTS)]
            summary[split][intent] = {
                "queries": len(selected), "paired_queries": len(paired),
                "source_groups": len({r["source_group"] for r in paired}),
                "scores": {v: {m: aggregate([r["metrics"][v] for r in paired]).get(m) for m in METRICS}
                           for v in VARIANTS},
                "queries_with_keyword_only_positive_at_10": {
                    v: sum(bool(r["keyword_only_positives_in_top10"][v]) for r in paired) for v in VARIANTS
                },
            }
    result = {
        "dataset_hash": frozen["dataset_hash"], "source_sha256": file_hash(SOURCE),
        "queries": len(rows), "reproduced_ik_rrf60_queries": len(rows),
        "variants": {
            "vector": "纯文本向量",
            "ik_rrf60": "当前 IK BM25 + 文本向量等权 RRF，k=60",
            "same_method_rrf10": "只在同解法查询中改用 k=10",
            "same_method_vector_top3": "只在同解法查询中先保留向量前 3，再按原 k=60 顺序补足",
        },
        "scope": "历史数据的事后回放，沿用冻结银标；不重新召回、不启用图文通道或重排。",
        "summary": summary, "per_query": rows,
    }
    write_json(TARGET, result)
    print(TARGET)
    return result


if __name__ == "__main__":
    compare()
