"""冻结配置下的候选池；不在建池阶段发布测试指标。"""

import random
from pathlib import Path
from .config import experiments, SEED, query_subset
from .files import read_rows, read_json, write_json, write_rows, digest, mutable
from .retrievers import Retriever


async def collect_pool(dataset, *, limit=None, smoke=False):
    dataset = Path(dataset)
    mutable(dataset)
    retriever = Retriever(dataset)
    try:
        retriever.require_index()
        queries = read_rows(dataset / "queries.jsonl")
        for query in query_subset(queries, limit, smoke):
            path = dataset / "work/pools" / (query["id"] + ".json")
            key = digest(
                [retriever.fingerprint, query, [e.as_dict() for e in experiments()]]
            )
            previous = read_json(path)
            if previous and previous.get("key") == key:
                continue
            vectors = await retriever.vectors(query, {"text_vector", "multimodal"})
            runs = {
                e.id: await retriever.retrieve(query, e, vectors) for e in experiments()
            }
            candidates = set(query["known_targets"])
            for run in runs.values():
                candidates.update(r["id"] for r in run["rankings"][:50])
            allowed = sorted(
                set(retriever.lookup) - candidates - set(query["excluded_ids"])
            )
            rng = random.Random(SEED + int(digest(query["id"])[:8], 16))
            random_ids = rng.sample(allowed, min(10, len(allowed)))
            candidates.update(random_ids)
            blinded = sorted(candidates)
            rng.shuffle(blinded)
            write_json(
                path,
                {
                    "key": key,
                    "query_id": query["id"],
                    "candidate_ids": blinded,
                    "random_ids": random_ids,
                    "runs": runs,
                },
            )
            print(f"pooled {query['id']} {len(blinded)}", flush=True)
        rows = []
        for q in queries:
            value = read_json(dataset / "work/pools" / (q["id"] + ".json"))
            if value:
                rows.append(
                    {k: value[k] for k in ("query_id", "candidate_ids", "random_ids")}
                )
        write_rows(dataset / "pools.jsonl", rows)
        return {"pooled_queries": len(rows), "expected": len(queries)}
    finally:
        await retriever.close()
