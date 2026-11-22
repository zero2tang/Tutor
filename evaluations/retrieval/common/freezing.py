"""只有快照、查询、候选池和标签共同满足约束才能冻结。"""

from pathlib import Path
from .dataset import validate_dataset, verify_assets
from .files import read_rows, read_json, write_json, file_hash, mutable, digest
from .config import experiment_hash, experiments
from .retrievers import representation_hash


def freeze(dataset):
    dataset = Path(dataset)
    mutable(dataset)
    validation = validate_dataset(dataset, require_labels=True)
    if not validation["valid"]:
        write_json(dataset / "review/validation.json", validation)
        raise ValueError("数据不完整，详见review/validation.json")
    corpus = read_rows(dataset / "corpus.jsonl")
    verify_assets(dataset, corpus)
    index = read_json(dataset / "index.json", {})
    if index.get("status") != "ready" or index.get("config", {}).get(
        "corpus_hash"
    ) != representation_hash(corpus):
        raise ValueError("图文索引未完成或不匹配当前语料")
    labels = read_rows(dataset / "qrels.jsonl")
    keys = [(r["query_id"], r["question_id"]) for r in labels]
    if len(keys) != len(set(keys)):
        raise ValueError("同一查询题目对存在重复标签")
    queries = {q["id"]: q for q in read_rows(dataset / "queries.jsonl")}
    qids = set(queries)
    cids = {r["id"] for r in corpus}
    if any(
        r["query_id"] not in qids
        or r["question_id"] not in cids
        or (
            r["grade"] is not None
            and (type(r["grade"]) is not int or r["grade"] not in range(4))
        )
        for r in labels
    ):
        raise ValueError("标签越界或等级无效")
    lookup = {(r["query_id"], r["question_id"]): r["grade"] for r in labels}
    pools = read_rows(dataset / "pools.jsonl")
    if {p["query_id"] for p in pools} != qids:
        raise ValueError("候选池未覆盖全部查询")
    for pool in pools:
        qid = pool["query_id"]
        detail = read_json(dataset / "work/pools" / (qid + ".json"))
        if not detail or set(detail["runs"]) != {e.id for e in experiments()}:
            raise ValueError("候选收集缺少实验")
        expected_key = digest(
            [index["fingerprint"], queries[qid], [e.as_dict() for e in experiments()]]
        )
        if (
            detail.get("key") != expected_key
            or detail["candidate_ids"] != pool["candidate_ids"]
        ):
            raise ValueError("候选池与当前查询或实验配置不一致")
        for run in detail["runs"].values():
            if (
                run["status"] != "completed"
                or run["fingerprint"] != index["fingerprint"]
            ):
                raise ValueError("候选池包含失败或过期实验")
            if any(lookup.get((qid, r["id"])) is None for r in run["rankings"][:20]):
                raise ValueError("正式前20候选有未解决标注：" + qid)
        if any((qid, cid) not in lookup for cid in pool["candidate_ids"]):
            raise ValueError("候选池尚未全部完成初标")
        if any(not r.get("review_complete") for r in labels if r["query_id"] == qid):
            raise ValueError("候选标签复判尚未完成")
    manifest = read_json(dataset / "manifest.json")
    manifest.update(status="frozen_silver", label_status="silver_not_human_reviewed")
    write_json(dataset / "manifest.json", manifest)
    files = {
        name: file_hash(dataset / name)
        for name in (
            "manifest.json",
            "request.json",
            "models.json",
            "corpus.jsonl",
            "queries.jsonl",
            "qrels.jsonl",
            "pools.jsonl",
            "index.json",
        )
    }
    for name in (
        "excluded.jsonl",
        "review/replacement_history.json",
        "subset_inputs.json",
        "subset_selection.json",
    ):
        if (dataset / name).exists():
            files[name] = file_hash(dataset / name)
    result = {
        "files": files,
        "dataset_hash": digest(files),
        "experiment_hash": experiment_hash(),
        "label_kind": "silver",
        "validation": validation,
        "unjudged_pool_pairs": sum(r["grade"] is None for r in labels),
    }
    write_json(dataset / "freeze.json", result)
    return result
