"""复算通道排名与RRF，并进行明确标记的事后敏感性分析。"""

import argparse
import math
from collections import defaultdict
from pathlib import Path

from ..common.config import HOME
from ..common.files import read_json, read_rows, verify_freeze, write_json
from ..common.metrics import aggregate, query_metrics


def fuse_saved(channels, constant=60, keyword_weight=1):
    scores = defaultdict(float)
    for name, hits in channels.items():
        weight = keyword_weight if name == "keyword" else 1
        assert len({hit["id"] for hit in hits}) == len(hits)
        for rank, hit in enumerate(hits, 1):
            scores[hit["id"]] += weight / (constant + rank)
    return sorted(scores, key=lambda cid: (-scores[cid], cid)), scores


def diagnose(run_id):
    run = HOME / "runs" / run_id
    config = read_json(run / "run.json")
    dataset = Path(config["dataset"])
    frozen = verify_freeze(dataset)
    if frozen["dataset_hash"] != config["dataset_hash"]:
        raise ValueError("运行和冻结数据不一致")
    queries = read_rows(dataset / "queries.jsonl")
    corpus = {c["id"]: c for c in read_rows(dataset / "corpus.jsonl")}
    judgments = read_rows(dataset / "qrels.jsonl")
    variants = (
        ("original_E1", None, None),
        ("original_E3", 60, 1),
        ("posthoc_rrf_k1", 1, 1),
        ("posthoc_rrf_k10", 10, 1),
        ("posthoc_rrf_k60_keyword025", 60, 0.25),
        ("posthoc_rrf_k60_keyword05", 60, 0.5),
    )
    variant_rows = defaultdict(list)
    result = {
        "run_id": run_id,
        "dataset_hash": frozen["dataset_hash"],
        "notice": "只读取保存的通道排名和冻结银标。事后变体用于解释机制，已接触测试集，不能作为新正式成绩或选参依据。",
        "queries": [],
    }
    for q in queries:
        records = {
            e: read_json(run / e / (q["id"] + ".json"))
            for e in ("E1", "E2", "E3", "E4")
        }
        if any(r["status"] != "completed" for r in records.values()):
            raise ValueError("存在未完成实验")
        channels = records["E3"]["result"]["channel_rankings"]
        # 不仅核对最终名次，也核对两个实验间通道原始分数完全相同。
        assert (
            channels["text_vector"]
            == records["E1"]["result"]["channel_rankings"]["text_vector"]
        )
        assert (
            channels["keyword"]
            == records["E2"]["result"]["channel_rankings"]["keyword"]
        )
        ids, scores = fuse_saved(channels)
        ranked = records["E3"]["result"]["rankings"]
        assert ids == [r["id"] for r in ranked]
        assert all(
            math.isclose(scores[r["id"]], r["score"], abs_tol=1e-12) for r in ranked
        )
        labels = {r["question_id"]: r for r in judgments if r["query_id"] == q["id"]}
        grades = {cid: r["grade"] for cid, r in labels.items()}
        hard = {cid for cid, r in labels.items() if r["near_miss"]}
        ranks = {
            e: {h["id"]: i for i, h in enumerate(r["result"]["rankings"], 1)}
            for e, r in records.items()
        }
        positives = [
            cid for cid, grade in grades.items() if grade is not None and grade >= 2
        ]
        common = set(h["id"] for h in channels["keyword"]) & set(
            h["id"] for h in channels["text_vector"]
        )
        e1, e3 = records["E1"]["metrics"], records["E3"]["metrics"]
        row = {
            "query_id": q["id"],
            "source_id": q["source_id"],
            "source_group": q["source_group"],
            "split": q["split"],
            "intent": q["intent"],
            "input_kind": q["input_kind"],
            "query_text": q["text"],
            "query_native_text": q["native_text"],
            "metrics": {e: r["metrics"] for e, r in records.items()},
            "recall10_delta": e3["recall_at_10"] - e1["recall_at_10"],
            "ndcg10_delta": e3["ndcg_at_10"] - e1["ndcg_at_10"],
            "intersection_size": len(common),
            "positive_count": len(positives),
            "vector_candidate_recall": len(set(ranks["E1"]) & set(positives))
            / len(positives),
            "hybrid_candidate_recall": len(set(ids) & set(positives)) / len(positives),
            "positives": [
                {
                    "id": cid,
                    "grade": grades[cid],
                    "reason": labels[cid]["reason"],
                    "candidate_native_text": corpus[cid]["native_text"],
                    "ranks": {e: r.get(cid) for e, r in ranks.items()},
                    "rrf_score": scores.get(cid),
                }
                for cid in positives
            ],
            "rrf_top10": [
                {
                    **h,
                    "position": i,
                    "grade": grades[h["id"]],
                    "candidate_native_text": corpus[h["id"]]["native_text"],
                    "reason": labels[h["id"]]["reason"],
                }
                for i, h in enumerate(ranked[:10], 1)
            ],
        }
        for name, constant, weight in variants:
            selected = (
                list(ranks["E1"])
                if constant is None
                else fuse_saved(channels, constant, weight)[0]
            )
            metrics = query_metrics(selected, grades, hard)
            assert metrics["label_complete"]
            variant_rows[name].append(
                {
                    "query_id": q["id"],
                    "source_group": q["source_group"],
                    "split": q["split"],
                    "metrics": metrics,
                }
            )
        result["queries"].append(row)
    result["variants"] = {
        name: {
            split: aggregate([r["metrics"] for r in rows if r["split"] == split])
            for split in ("development", "test")
        }
        for name, rows in variant_rows.items()
    }
    test = [q for q in result["queries"] if q["split"] == "test"]
    families = defaultdict(list)
    for q in test:
        families[q["source_group"]].append(q)
    result["test_family_contributions"] = [
        {
            "source_id": rows[0]["source_id"],
            "intent": rows[0]["intent"],
            "queries": len(rows),
            "recall10_delta_sum": sum(r["recall10_delta"] for r in rows),
            "recall10_macro_contribution": sum(r["recall10_delta"] for r in rows)
            / len(test),
            "ndcg10_macro_contribution": sum(r["ndcg10_delta"] for r in rows)
            / len(test),
        }
        for rows in families.values()
    ]
    result["verification"] = {
        "channel_lists_and_scores_identical": True,
        "rrf_recomputed_queries": len(queries),
        "rrf_formula_matches_saved_results": True,
    }
    target = HOME / "reports" / (run_id + "_hybrid_diagnosis.json")
    write_json(target, result)
    return result, target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default="math_100_q24_v1_initial")
    args = parser.parse_args()
    result, target = diagnose(args.run_id)
    print(target)
    print(result["verification"])
    print(result["test_family_contributions"])
    for name, by_split in result["variants"].items():
        print(
            name,
            {
                split: {m: round(values[m], 6) for m in ("recall_at_10", "ndcg_at_10")}
                for split, values in by_split.items()
            },
        )


if __name__ == "__main__":
    main()
