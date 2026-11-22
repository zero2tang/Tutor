"""每通道按题目聚合后融合，多图和分块不产生重复票。"""


def fuse(channels, k=60, *, by_question=True, exact_ids=(), stable_order=False):
    records = {}
    for channel, hits in channels.items():
        seen = set()
        rank = 0
        for hit in hits:
            source = hit.get("_source", {})
            identifier = (source.get("question_id") or hit["_id"]) if by_question else hit["_id"]
            if identifier in seen:
                continue
            seen.add(identifier)
            rank += 1
            item = records.setdefault(identifier, {"id": identifier, "rrf_score": 0, "ranks": {}, "raw_scores": {}, "source": source})
            item["source"] = source
            item["rrf_score"] += 1 / (k + rank)
            item["ranks"][channel] = rank
            item["raw_scores"][channel] = hit.get("_score")
    return sorted(records.values(), key=lambda r: (r["id"] not in exact_ids, -r["rrf_score"], "" if stable_order else r["id"]))
