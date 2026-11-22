"""固定候选，对冻结题库实测本地 Qwen 精排。"""

import argparse
from collections import defaultdict
import math
from pathlib import Path
import statistics
import time

from PIL import Image
import torch
import transformers

from ..common.config import DEFAULT_DATASET, HOME
from ..common.files import file_hash, read_json, read_rows, verify_freeze, write_json
from .download_qwen import CACHE, MODELS
from local_reranker.scorer import INSTRUCTIONS, MAX_IMAGE_PIXELS, QwenScorer


SOURCE = HOME / "reports/math_100_q24_v1_ik_replay.json"


def quality(ids, labels, k):
    head = ids[:k]
    unjudged = [cid for cid in head if labels.get(cid) is None]
    positives = {cid for cid, grade in labels.items() if grade is not None and grade >= 2}
    if unjudged or not positives:
        return {"unjudged": unjudged, "recall": None, "ndcg": None}
    def dcg(grades):
        return sum((2 ** grade - 1) / math.log2(i + 2) for i, grade in enumerate(grades))
    ideal = dcg(sorted((grade for grade in labels.values() if grade is not None), reverse=True)[:k])
    return {"unjudged": [], "recall": len(positives.intersection(head)) / len(positives),
            "ndcg": dcg([labels[cid] for cid in head]) / ideal}


def compare(rows, variants):
    out = {}
    for group, selected in (
        ("all", rows),
        ("test", [row for row in rows if row["split"] == "test"]),
        ("test_same_method", [row for row in rows if row["split"] == "test" and row["intent"] == "same_method"]),
        ("test_text", [row for row in rows if row["split"] == "test" and row["input_kind"] == "text"]),
        ("test_image", [row for row in rows if row["split"] == "test" and row["input_kind"] == "image"]),
    ):
        out[group] = {}
        for k in ("5", "10"):
            paired = [row for row in selected if all(row["metrics"][v][k]["ndcg"] is not None for v in variants)]
            out[group][k] = {"paired_queries": len(paired), "queries": len(selected),
                             "scores": {v: {m: statistics.mean(row["metrics"][v][k][m] for row in paired) if paired else None
                                             for m in ("recall", "ndcg")} for v in variants}}
    return out


def run(kind, limit):
    torch.set_num_threads(4)
    visual = kind == "vl"
    batch_size = 2 if visual else 8
    frozen = verify_freeze(DEFAULT_DATASET)
    previous = read_json(SOURCE)
    if previous["dataset_hash"] != frozen["dataset_hash"]:
        raise ValueError("候选和冻结数据不是同一版本")
    by_qid = {r["query_id"]: r for r in previous["per_query"]}
    queries = read_rows(DEFAULT_DATASET / "queries.jsonl")
    corpus = {row["id"]: row for row in read_rows(DEFAULT_DATASET / "corpus.jsonl")}
    if len(queries) != 24 or len(corpus) != 100 or set(by_qid) != {q["id"] for q in queries}:
        raise ValueError("必须读取原 100 题、24 查询的冻结版本")
    labels = defaultdict(dict)
    for row in read_rows(DEFAULT_DATASET / "qrels.jsonl"):
        labels[row["query_id"]][row["question_id"]] = row["grade"]

    image_cache = {}
    def payload(row):
        images = []
        if visual:
            for asset in row["assets"]:
                path = DEFAULT_DATASET / "assets" / Path(asset["location"]).name
                sha = asset["content_hash"]
                if sha not in image_cache:
                    if file_hash(path) != sha:
                        raise ValueError(f"冻结题图内容校验失败：{path.name}")
                    with Image.open(path) as image:
                        image_cache[sha] = image.convert("RGB")
                images.append(image_cache[sha])
        return {"text": row["text"], "images": images}

    if limit:
        # 冒烟同时覆盖纯文字和含图查询。
        queries = [next(q for q in queries if q["input_kind"] == k) for k in ("text", "image")][:limit]
    cases = []
    for q in queries:
        prior = by_qid[q["id"]]
        vector = prior["rankings"]["E1"]
        keyword = [hit["id"] for hit in prior["keyword_rankings"]["IK"]]
        selected = list(dict.fromkeys(vector[:12] + keyword + vector[12:]))[:20]
        if len(selected) != 20 or set(q["excluded_ids"]) & set(selected):
            raise ValueError("精排名单不足 20 或含被排除题")
        selected_set = set(selected)
        rrf_same = [cid for cid in prior["rankings"]["E3_IK"] if cid in selected_set]
        if set(rrf_same) != selected_set:
            raise ValueError("同候选 RRF 对照未覆盖精排名单")
        cases.append((q, prior, selected, rrf_same, payload(q), [payload(corpus[cid]) for cid in selected]))

    model = MODELS[int(visual)]
    folder = CACHE / model
    receipt = read_json(folder / "tutor_snapshot.json")
    if not receipt or not (folder / "model.safetensors").is_file():
        raise ValueError(f"{model} 未下载并校验完成")
    print(f"loading {model}", flush=True)
    scorer = QwenScorer(folder, visual)
    q, _, _, _, warm_q, warm_docs = max(cases, key=lambda c: len(c[4]["images"]) + max(len(d["images"]) for d in c[5]))
    warm_docs = sorted(warm_docs, key=lambda d: -len(d["images"]))[:batch_size]
    scorer.score(warm_q, warm_docs, INSTRUCTIONS[q["intent"]], batch_size)
    torch.cuda.synchronize()

    rows = []
    variants = ("vector", "ik_rrf", "shortlist_rrf", kind)
    for q, prior, selected, rrf_same, query, docs in cases:
        torch.cuda.reset_peak_memory_stats()
        start = time.perf_counter()
        scores, tokens, images = scorer.score(query, docs, INSTRUCTIONS[q["intent"]], batch_size)
        torch.cuda.synchronize()
        elapsed = (time.perf_counter() - start) * 1000
        ranking = [cid for cid, _ in sorted(zip(selected, scores), key=lambda x: -x[1])]
        order = dict(zip(variants, (prior["rankings"]["E1"], prior["rankings"]["E3_IK"], rrf_same, ranking)))
        judgments = labels[q["id"]]
        positives = {cid for cid, grade in judgments.items() if grade is not None and grade >= 2}
        rows.append({
            "query_id": q["id"], "intent": q["intent"], "split": q["split"],
            "input_kind": q["input_kind"], "source_id": q["source_id"],
            "candidates": selected, "scores": dict(zip(selected, scores)), "rankings": order,
            "metrics": {v: {str(k): quality(ids, judgments, k) for k in (5, 10)} for v, ids in order.items()},
            "candidate_recall": len(positives.intersection(selected)) / len(positives),
            "rank_of_positive": {cid: ranking.index(cid) + 1 if cid in ranking else None for cid in sorted(positives)},
            "latency_ms": elapsed, "peak_allocated_mib": torch.cuda.max_memory_allocated() / 2 ** 20,
            "peak_reserved_mib": torch.cuda.max_memory_reserved() / 2 ** 20,
            "max_tokens": tokens, "images_scored": images,
        })
        print(f"{kind} {len(rows)}/{len(cases)} {q['id']} {elapsed:.0f} ms; images={images}", flush=True)

    latencies = sorted(row["latency_ms"] for row in rows)
    result = {
        "model": model, "dataset_hash": frozen["dataset_hash"], "candidate_source_sha256": file_hash(SOURCE),
        "weight_sha256": receipt["files"]["model.safetensors"]["sha256"],
        "gpu": torch.cuda.get_device_name(), "torch": torch.__version__, "transformers": transformers.__version__,
        "dtype": "bf16", "batch_size": batch_size, "max_image_pixels": MAX_IMAGE_PIXELS if visual else None,
        "candidate_count": 20, "queries": len(rows), "scored_pairs": 20 * len(rows),
        "summary": compare(rows, variants), "timing": {
            "p50_ms": statistics.median(latencies), "p95_ms_nearest_rank": latencies[math.ceil(.95 * len(latencies)) - 1],
            "peak_allocated_mib": max(r["peak_allocated_mib"] for r in rows),
            "peak_reserved_mib": max(r["peak_reserved_mib"] for r in rows),
            "images_scored": sum(r["images_scored"] for r in rows),
        },
        "per_query": rows,
    }
    target = HOME / f"reports/math_100_q24_v1_qwen_{kind}{'_smoke' if limit else ''}.json"
    write_json(target, result)
    print(target, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", choices=["text", "vl"])
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    run(args.model, 2 if args.smoke else None)
