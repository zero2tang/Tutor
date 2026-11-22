"""题目级确定性指标；未知标签不能被伪装为负例。"""

import math
import random
import statistics


def query_metrics(ids, labels, hard_negatives=()):
    ids = list(dict.fromkeys(ids))
    relevant = {
        qid for qid, grade in labels.items() if grade is not None and grade >= 2
    }
    top = ids[:20]
    judged = sum(labels.get(qid) is not None for qid in top)
    complete = judged == len(top) and bool(relevant)
    result = {
        "judged_at_20": judged / len(top) if top else 1.0,
        "returned": len(top),
        "reference_positives": len(relevant),
        "label_complete": complete,
        "hard_negative_at_10": sum(q in hard_negatives for q in ids[:10]) / 10,
    }
    for k in (1, 5, 10, 20):
        hits = sum(qid in relevant for qid in ids[:k])
        result[f"recall_at_{k}"] = hits / len(relevant) if complete else None
        result[f"precision_at_{k}"] = hits / k if complete else None
        result[f"hit_at_{k}"] = int(hits > 0) if complete else None
        result[f"id_precision_at_{k}"] = (
            hits / len(ids[:k]) if complete and ids[:k] else 0.0 if complete else None
        )
    result["mrr_at_10"] = (
        next((1 / (i + 1) for i, q in enumerate(ids[:10]) if q in relevant), 0.0)
        if complete
        else None
    )

    def dcg(grades):
        return sum((2**grade - 1) / math.log2(i + 2) for i, grade in enumerate(grades))

    ideal = dcg(
        sorted((v for v in labels.values() if v is not None), reverse=True)[:10]
    )
    result["ndcg_at_10"] = (
        dcg([labels[q] for q in ids[:10]]) / ideal if complete and ideal else None
    )
    return result


def aggregate(rows):
    keys = sorted(
        {
            k
            for row in rows
            for k, v in row.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        }
    )
    return {
        "queries": len(rows),
        **{
            k: (
                statistics.mean([r[k] for r in rows if r.get(k) is not None])
                if any(r.get(k) is not None for r in rows)
                else None
            )
            for k in keys
        },
    }


def paired_bootstrap(rows, left, right, metric, repeats=2000, seed=20261023):
    """以来源题族为抽样单元，保持同族两条查询和实验间的配对关系。"""
    lookup = {(r["experiment"], r["query_id"]): r for r in rows}
    families = {}
    for row in rows:
        if row["experiment"] != left:
            continue
        other = lookup.get((right, row["query_id"]))
        a, b = row["metrics"].get(metric), (other or {}).get("metrics", {}).get(metric)
        if a is not None and b is not None:
            families.setdefault(row["source_group"], []).append(a - b)
    if not families:
        return {"available": False}
    clusters, rng = list(families.values()), random.Random(seed)
    samples = []
    for _ in range(repeats):
        values = [
            v for cluster in rng.choices(clusters, k=len(clusters)) for v in cluster
        ]
        samples.append(statistics.mean(values))
    samples.sort()
    return {
        "available": True,
        "difference": statistics.mean([v for c in clusters for v in c]),
        "ci95": [
            samples[int(0.025 * repeats)],
            samples[min(repeats - 1, int(0.975 * repeats))],
        ],
        "source_groups": len(clusters),
        "repeats": repeats,
    }
