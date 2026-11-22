"""在冻结的 100 题、24 查询实验中，仅替换关键词分析器。"""

import asyncio
from collections import defaultdict
from copy import deepcopy
from urllib.parse import quote, urlsplit

from app.domains.courses.questions.search.representation import text_views
from app.infrastructure.clients.retrieval_index import RetrievalIndex
from app.infrastructure.clients.search import SearchClient
from app.infrastructure.clients.search_analysis import IK_TEXT_ANALYSIS

from .. import ROOT
from ..common.config import DEFAULT_DATASET, HOME, experiment_hash, experiments
from ..common.files import digest, file_hash, read_json, read_rows, verify_freeze, write_json
from ..common.metrics import aggregate, paired_bootstrap, query_metrics
from .hybrid_diagnosis import fuse_saved


ORIGINAL = "math_100_q24_v1_initial"
REPORT = HOME / "reports/math_100_q24_v1_ik_replay.json"
METRICS = ("recall_at_5", "recall_at_10", "recall_at_20", "ndcg_at_10", "precision_at_10", "mrr_at_10")
SCORE_AT = {"recall_at_5": 5, "recall_at_20": 20}


async def clone_keyword_index(search, source, expected, dictionaries):
    old = (await search.request("GET", f"/{source}"))[source]
    settings = old["settings"]["index"]
    mapping = deepcopy(old["mappings"])
    if mapping["properties"]["text"].get("analyzer") != "cjk":
        raise ValueError("原实验索引没有使用 CJK 分词")
    mapping["properties"]["text"].update(IK_TEXT_ANALYSIS)
    target = source + "_ik_" + digest([settings["uuid"], dictionaries, mapping])[:12]
    response = await search.http.head(f"/{target}")
    if response.status_code == 404:
        # 单独建立实验索引；保留原映射、向量和文档 ID，不调用编码器。
        copy_settings = {k: v for k, v in settings.items() if k not in {
            "creation_date", "provided_name", "uuid", "version", "history_uuid"
        }}
        await search.request("PUT", f"/{target}", json={
            "settings": {"index": copy_settings}, "mappings": mapping
        })
        result = await search.request("POST", "/_reindex", params={"refresh": "true"}, json={
            "source": {"index": source}, "dest": {"index": target, "op_type": "create"},
        })
        if result["failures"] or result["created"] != len(expected) or result["timed_out"]:
            raise ValueError(f"实验索引复制失败：{result}")
    else:
        response.raise_for_status()
    actual = (await search.request("GET", f"/{target}/_mapping"))[target]["mappings"]
    if actual != mapping:
        raise ValueError("实验索引映射与 IK 对照配置不一致")

    async def snapshot(index):
        hits = (await search.request("POST", f"/{index}/_search", json={
            "size": len(expected), "track_total_hits": True, "_source": {"exclude_vectors": False},
        }))["hits"]
        if hits["total"]["value"] != len(expected):
            raise ValueError(f"{index} 未包含全部冻结文档")
        return {h["_id"]: h["_source"] for h in hits["hits"]}

    before, after = await asyncio.gather(snapshot(source), snapshot(target))
    if before != after or set(before) != set(expected) or any(
        (row["question_id"], row["source_hash"], row["text"]) != expected[identifier]
        or not row.get("embedding")
        for identifier, row in before.items()
    ):
        raise ValueError("CJK、IK 和冻结题库的题面、版本或向量不一致")
    return target


async def keywords(index, name, q, corpus, candidate_limit, fetch_limit):
    excluded = set(q.get("excluded_ids", []))
    scope = {"bool": {
        "filter": [{"term": {"course_id": "math"}}, {"term": {"visibility": "public"}}],
        "must_not": [{"terms": {"question_id": sorted(excluded)}}] if excluded else [],
    }}
    response = await index.keyword(name, scope, q["text"], fetch_limit)
    unique = {}
    for hit in response["hits"]["hits"]:
        row = hit["_source"]
        cid = row["question_id"]
        if cid in excluded or cid not in corpus or row["source_hash"] != corpus[cid]["source_hash"]:
            raise ValueError("召回了快照外、过期或排除范围内的题目")
        unique.setdefault(cid, {"id": cid, "score": hit["_score"]})
        if len(unique) == candidate_limit:
            break
    return list(unique.values())


async def sum_case_evidence(search, index, indices, queries, corpus, expected):
    """只在原退化案例中额外核对全文匹配和截断前的关键词名次。"""
    target_id = "math:1049"
    document_ids = [doc_id for doc_id, (cid, _, _) in expected.items() if cid == target_id]
    cases = {}
    for q in queries:
        if q["source_id"] != "math:1587":
            continue
        per_analyzer = {}
        for label, name in indices.items():
            all_hits = await keywords(index, name, q, corpus, len(corpus), len(expected))
            explanations = {}
            for doc_id in document_ids:
                response = await search.request("POST", f"/{name}/_explain/{quote(doc_id, safe='')}",
                                                json={"query": {"match": {"text": q["text"]}}})
                explanations[doc_id] = {k: response[k] for k in ("matched", "explanation")}
            per_analyzer[label] = {
                "keyword_rank_before_cutoff": next(
                    (i for i, hit in enumerate(all_hits, 1) if hit["id"] == target_id), None
                ),
                "explanations": explanations,
            }
        cases[q["id"]] = per_analyzer
    return cases


def summarize(rows, variants):
    result = {}
    for split in ("development", "test", "all"):
        selected = [r for r in rows if split == "all" or r["split"] == split]
        metrics = {}
        for metric in METRICS:
            cutoff = SCORE_AT.get(metric, 10)
            # 每个指标只在所有对照组都已标注的同一批查询上计算。
            paired = [
                r for r in selected if all(not r["unknown_at"][v][cutoff] for v in variants)
            ]
            metrics[metric] = {
                "paired_queries": len(paired), "total_queries": len(selected),
                "values": {v: aggregate([r["metrics"][v][cutoff] for r in paired]).get(metric)
                           for v in variants},
            }
        result[split] = metrics
    return result


async def replay():
    frozen = verify_freeze(DEFAULT_DATASET)
    run = HOME / "runs" / ORIGINAL
    metadata = read_json(run / "run.json")
    if metadata["dataset_hash"] != frozen["dataset_hash"] or any(
        value != experiment_hash() for value in (metadata["experiment_hash"], frozen["experiment_hash"])
    ):
        raise ValueError("原实验和冻结数据或实验配置不一致")
    queries = read_rows(DEFAULT_DATASET / "queries.jsonl")
    corpus = {row["id"]: row for row in read_rows(DEFAULT_DATASET / "corpus.jsonl")}
    if len(corpus) != 100 or len(queries) != 24:
        raise ValueError("评测必须使用同一批 100 题、24 条查询")
    labels, hard = defaultdict(dict), defaultdict(set)
    for judgment in read_rows(DEFAULT_DATASET / "qrels.jsonl"):
        qid, cid = judgment["query_id"], judgment["question_id"]
        labels[qid][cid] = judgment["grade"]
        if judgment["near_miss"]:
            hard[qid].add(cid)

    source = read_json(DEFAULT_DATASET / "index.json")["indices"][0]
    dictionary_dir = ROOT / "deploy/elasticsearch/ik"
    dictionaries = {path.name: file_hash(path) for path in sorted(dictionary_dir.glob("*.dic"))}
    expected = {f"{cid}:{i}": (cid, row["source_hash"], text)
                for cid, row in corpus.items() for i, text in enumerate(text_views(row["text"]))}
    configs = {e.id: e for e in experiments()}
    keyword_variants = ("E2_CJK", "E2_IK")
    fusion_variants = ("E1", "E3_CJK", "E3_IK", "E4_CJK", "E4_IK")
    variants = keyword_variants + fusion_variants
    rows = []
    search = SearchClient()
    try:
        if urlsplit(str(search.http.base_url)).hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("仅允许在本机 ES 上创建评测索引")
        plugins = (await search.request("GET", "/_cat/plugins?format=json"))
        ik_versions = sorted({p["version"] for p in plugins if p["component"] == "analysis-ik"})
        if not ik_versions:
            raise ValueError("本机 ES 未加载 IK 插件")
        target = await clone_keyword_index(search, source, expected, dictionaries)
        retrieval = RetrievalIndex(search)
        fetch_limit = 50 * max(len(text_views(row["text"])) for row in corpus.values())
        for q in queries:
            qid = q["id"]
            records = {e: read_json(run / e / (qid + ".json")) for e in configs}
            if any(r["status"] != "completed" for r in records.values()):
                raise ValueError(f"原实验结果未完成：{qid}")
            cjk, ik = await asyncio.gather(*(
                keywords(retrieval, name, q, corpus, configs["E2"].candidate_limit, fetch_limit)
                for name in (source, target)
            ))
            if cjk != records["E2"]["result"]["channel_rankings"]["keyword"]:
                raise ValueError(f"CJK 实时回放与原关键词结果不一致：{qid}")
            rankings = {"E1": [r["id"] for r in records["E1"]["result"]["rankings"]]}
            intersections = {}
            for e in ("E2", "E3", "E4"):
                baseline = records[e]["result"]["channel_rankings"]
                if cjk != baseline["keyword"]:
                    raise ValueError(f"原实验关键词通道不一致：{qid} {e}")
                rankings[e + "_CJK"] = fuse_saved(baseline, configs[e].rrf_k)[0]
                if rankings[e + "_CJK"] != [r["id"] for r in records[e]["result"]["rankings"]]:
                    raise ValueError(f"原实验融合结果无法复现：{qid} {e}")
                rankings[e + "_IK"] = fuse_saved({**baseline, "keyword": ik}, configs[e].rrf_k)[0]
                if e == "E3":
                    text = {r["id"] for r in baseline["text_vector"]}
                    intersections = {"CJK": len(text.intersection(r["id"] for r in cjk)),
                                     "IK": len(text.intersection(r["id"] for r in ik))}
            rows.append({
                "query_id": qid, "text": q["text"], "split": q["split"],
                "source_group": q["source_group"], "source_id": q["source_id"],
                "intent": q["intent"], "input_kind": q["input_kind"],
                "keyword_rankings": {"CJK": cjk, "IK": ik}, "rankings": rankings,
                "positive_ids": [cid for cid, g in labels[qid].items() if g is not None and g >= 2],
                "intersections_with_text_vector": intersections,
                "unknown_at": {v: {k: [cid for cid in ids[:k] if labels[qid].get(cid) is None]
                                   for k in (5, 10, 20)} for v, ids in rankings.items()},
                "metrics": {v: {k: query_metrics(ids[:k], labels[qid], hard[qid])
                                 for k in (5, 10, 20)} for v, ids in rankings.items()},
            })
        indices = {"CJK": source, "IK": target}
        evidence = await sum_case_evidence(search, retrieval, indices, queries, corpus, expected)
    finally:
        await search.close()
    test = [{"experiment": v, "query_id": r["query_id"], "source_group": r["source_group"],
             "metrics": r["metrics"][v][10]}
            for r in rows if r["split"] == "test" for v in variants]
    result = {
        "baseline_run": ORIGINAL, "dataset_hash": frozen["dataset_hash"],
        "indices": indices, "ik_versions": ik_versions,
        "dictionary_sha256": dictionaries, "analysis": IK_TEXT_ANALYSIS,
        "queries": len(queries), "documents": len(expected), "remote_model_calls": 0,
        "reproduced_baseline_queries": len(rows), "summary": {
            "keyword": summarize(rows, keyword_variants),
            "fusion": summarize(rows, fusion_variants),
        },
        "test_pair_bootstrap": {f"{a}_minus_{b}": {
            metric: paired_bootstrap(test, a, b, metric) for metric in ("recall_at_10", "ndcg_at_10")
        } for a, b in (("E3_IK", "E3_CJK"), ("E3_IK", "E1"))},
        "original_sum_family": evidence, "per_query": rows,
    }
    write_json(REPORT, result)
    print(REPORT)


if __name__ == "__main__":
    asyncio.run(replay())
