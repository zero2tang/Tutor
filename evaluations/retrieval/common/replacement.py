"""只在冻结前按无正例证据补齐题族，不读取任何实验指标。"""

import random
from pathlib import Path
from .files import read_rows, read_json, write_rows, write_json, mutable, digest
from .config import SEED


def apply_replacement_state(dataset, state):
    for name, rows in state["rows"].items():
        write_rows(dataset / name, rows)
    for name, value in state["json"].items():
        write_json(dataset / name, value)
    state["status"] = "completed"
    write_json(dataset / "work/replacement_transaction.json", state)


def repair_missing_positives(dataset):
    dataset = Path(dataset)
    mutable(dataset)
    transaction = read_json(dataset / "work/replacement_transaction.json", {})
    if transaction.get("status") == "applying":
        apply_replacement_state(dataset, transaction)
        return {
            "recovered_transaction": True,
            "next": "prepare -> pool -> label -> repair",
        }
    corpus = read_rows(dataset / "corpus.jsonl")
    queries = read_rows(dataset / "queries.jsonl")
    labels = read_rows(dataset / "qrels.jsonl")
    pools = read_rows(dataset / "pools.jsonl")
    seeds = read_json(dataset / "work/query_seeds.json", [])
    by_pool = {p["query_id"]: p for p in pools}
    history = read_json(dataset / "review/replacement_history.json", [])
    expanded, blocked, bad_families = [], [], set()
    for q in queries:
        pool = by_pool.get(q["id"])
        if not pool:
            continue
        judgments = {r["question_id"]: r for r in labels if r["query_id"] == q["id"]}
        if any(r["grade"] is not None and r["grade"] >= 2 for r in judgments.values()):
            continue
        if any(
            cid not in judgments or not judgments[cid].get("review_complete")
            for cid in pool["candidate_ids"]
        ):
            blocked.append({"query_id": q["id"], "reason": "labels_incomplete"})
            continue
        missing = sorted(
            {r["id"] for r in corpus}
            - set(q["excluded_ids"])
            - set(pool["candidate_ids"])
        )
        if missing:
            # 100题域内先补查池外全部候选，避免因为池不完整而替换难查询。
            pool["candidate_ids"].extend(missing)
            random.Random(SEED + int(digest(q["id"])[:8], 16)).shuffle(
                pool["candidate_ids"]
            )
            detail_path = dataset / "work/pools" / (q["id"] + ".json")
            detail = read_json(detail_path)
            detail.update(
                candidate_ids=pool["candidate_ids"], positive_search_expansion=True
            )
            write_json(detail_path, detail)
            expanded.append(q["id"])
        elif any(r["grade"] is None for r in judgments.values()):
            blocked.append(
                {"query_id": q["id"], "reason": "unresolved_labels_require_review"}
            )
        else:
            bad_families.add(q["source_group"])
    if expanded:
        write_rows(dataset / "pools.jsonl", pools)
        return {
            "expanded_queries": expanded,
            "replaced_families": [],
            "blocked": blocked,
            "next": "label -> repair（先补标，暂不换题）",
        }
    unresolved_families = {
        q["source_group"]
        for q in queries
        if any(b.get("query_id") == q["id"] for b in blocked) or q["id"] not in by_pool
    }
    bad_families -= unresolved_families
    used = {q["source_group"] for q in queries} | {r["old_family"] for r in history}
    replacements = {}
    for family in sorted(bad_families):
        seed = next(s for s in seeds if s["source_group"] == family)
        candidates = sorted(
            [
                r
                for r in corpus
                if r["source_group"] not in used
                and r["has_image"] == (seed["input_kind"] == "image")
            ],
            key=lambda r: r["id"],
        )
        if not candidates:
            blocked.append(
                {
                    "source_group": family,
                    "reason": "no_unused_source_family_in_same_modality",
                }
            )
            continue
        random.Random(SEED + int(digest([family, len(history)])[:8], 16)).shuffle(
            candidates
        )
        chosen = candidates[0]
        old_queries = [q for q in queries if q["source_group"] == family]
        old_ids = {q["id"] for q in old_queries}
        archive = {
            "queries": old_queries,
            "labels": [r for r in labels if r["query_id"] in old_ids],
            "pools": [p for p in pools if p["query_id"] in old_ids],
        }
        write_json(dataset / "review/replaced" / (family + ".json"), archive)
        replacement = {
            **seed,
            "source_id": chosen["id"],
            "source_group": chosen["source_group"],
        }
        replacements[family] = replacement
        history.append(
            {
                "old_family": family,
                "new_family": chosen["source_group"],
                "new_source": chosen["id"],
                "intent": seed["intent"],
                "input_kind": seed["input_kind"],
                "split": seed["split"],
                "reason": "fully_judged_candidate_domain_has_no_positive",
                "scores_read": False,
            }
        )
        used.add(chosen["source_group"])
    if replacements:
        active = {q["id"] for q in queries if q["source_group"] not in replacements}
        manifest = read_json(dataset / "manifest.json")
        manifest["status"] = "queries_partial_after_predefined_replacement"
        state = {
            "status": "applying",
            "rows": {
                "queries.jsonl": [q for q in queries if q["id"] in active],
                "qrels.jsonl": [r for r in labels if r["query_id"] in active],
                "pools.jsonl": [p for p in pools if p["query_id"] in active],
            },
            "json": {
                "work/query_seeds.json": [
                    replacements.get(s["source_group"], s) for s in seeds
                ],
                "review/replacement_history.json": history,
                "manifest.json": manifest,
            },
        }
        write_json(dataset / "work/replacement_transaction.json", state)
        apply_replacement_state(dataset, state)
    return {
        "expanded_queries": [],
        "replaced_families": list(replacements),
        "blocked": blocked,
        "next": (
            "prepare -> pool -> label -> repair -> freeze"
            if replacements
            else "freeze 或人工复核缺口"
        ),
    }
