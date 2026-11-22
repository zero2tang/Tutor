"""在不改变候选题库的前提下派生小样本；筛选只读取标注，不读取实验排名。"""

import copy
import random
import shutil
from collections import defaultdict
from pathlib import Path
from .config import INTENTS, SEED
from .dataset import validate_text, verify_assets
from .files import (
    read_json,
    read_rows,
    write_json,
    write_rows,
    file_hash,
    digest,
    mutable,
    pin,
)
from .retrievers import representation_hash


def eligible_families(queries, labels, pools):
    families = defaultdict(list)
    judgments = {(r["query_id"], r["question_id"]): r for r in labels}
    candidates = {r["query_id"]: r["candidate_ids"] for r in pools}
    for q in queries:
        families[q["source_group"]].append(q)
    eligible = []
    for family in families.values():
        if len(family) != 2:
            continue
        if all(
            candidates.get(q["id"])
            and all(
                judgments.get((q["id"], cid), {}).get("review_complete")
                and judgments[(q["id"], cid)].get("grade") is not None
                for cid in candidates[q["id"]]
            )
            and any(
                (r.get("grade") or 0) >= 2
                for (qid, _), r in judgments.items()
                if qid == q["id"]
            )
            for q in family
        ):
            eligible.append(family)
    return eligible


def derive_subset(parent, dataset, plan_path):
    parent, dataset = Path(parent).resolve(), Path(dataset).resolve()
    if parent == dataset:
        raise ValueError("必须派生到新版本目录")
    mutable(dataset)
    plan = read_json(plan_path)
    corpus = read_rows(parent / "corpus.jsonl")
    if len(corpus) != 100 or plan.get("query_count") != 24:
        raise ValueError("本派生协议固定100道候选题、24条查询")
    verify_assets(parent, corpus)
    names = (
        "corpus.jsonl",
        "queries.jsonl",
        "qrels.jsonl",
        "pools.jsonl",
        "models.json",
        "index.json",
    )
    inputs = {
        "parent": str(parent),
        "files": {n: file_hash(parent / n) for n in names},
        "plan": plan,
    }
    pin(dataset / "subset_inputs.json", inputs)
    if (dataset / "subset_selection.json").exists():
        return read_json(dataset / "subset_selection.json")
    lookup = {r["id"]: r for r in corpus}
    old_queries = read_rows(parent / "queries.jsonl")
    labels = read_rows(parent / "qrels.jsonl")
    eligible = eligible_families(old_queries, labels, read_rows(parent / "pools.jsonl"))
    families, used = defaultdict(list), set()
    for item in plan.get("new_families", []):
        source = lookup[item["source_id"]]
        if source["has_image"] or source["source_group"] in used:
            raise ValueError("新增查询须为不同的纯文字来源题族")
        texts = item["queries"]
        if len(texts) != 2 or len(set(texts)) != 2:
            raise ValueError("每个来源题族需要两条不同表达")
        family = []
        for text in texts:
            validate_text(text)
            family.append(
                {
                    "source_id": source["id"],
                    "source_group": source["source_group"],
                    "intent": item["intent"],
                    "input_kind": "text",
                    "id": "q_"
                    + digest([source["source_group"], item["intent"], text])[:16],
                    "module": source["module"],
                    "native_text": text,
                    "text": text,
                    "assets": [],
                    "snapshot": {"stem": text, "options": {}},
                    "known_targets": (
                        [source["id"]] if item["intent"] == "exact" else []
                    ),
                    "excluded_ids": (
                        [
                            r["id"]
                            for r in corpus
                            if r["duplicate_key"] == source["duplicate_key"]
                        ]
                        if item["intent"] != "exact"
                        else []
                    ),
                    "transcription": None,
                }
            )
        families[item["intent"], "text"].append(family)
        used.add(source["source_group"])
    rng = random.Random(SEED)
    for intent in INTENTS:
        for modality in ("text", "image"):
            cell = (intent, modality)
            options = sorted(
                [
                    f
                    for f in eligible
                    if (f[0]["intent"], f[0]["input_kind"]) == cell
                    and f[0]["source_group"] not in used
                ],
                key=lambda f: f[0]["source_group"],
            )
            rng.shuffle(options)
            needed = 2 - len(families[cell])
            if needed < 0 or len(options) < needed:
                raise ValueError("缺少可复用的正例题族：" + "/".join(cell))
            for f in options[:needed]:
                families[cell].append(copy.deepcopy(f))
                used.add(f[0]["source_group"])
    queries, audit = [], []
    reused_ids = {q["id"] for q in old_queries}
    for cell, selected in families.items():
        selected.sort(key=lambda f: f[0]["source_group"])
        rng.shuffle(selected)
        for index, family in enumerate(selected):
            split = "development" if index == 0 else "test"
            audit.append(
                {
                    "source_id": family[0]["source_id"],
                    "source_group": family[0]["source_group"],
                    "intent": cell[0],
                    "input_kind": cell[1],
                    "split": split,
                    "parent_split": family[0].get("split"),
                    "query_origin": (
                        "reused"
                        if family[0]["id"] in reused_ids
                        else "ai_authored_public_surface"
                    ),
                }
            )
            for q in family:
                q["split"] = split
                queries.append(q)

    # 图片按内容哈希复制；索引字段和图像字节保持不变，不重新编码候选题。
    def relocate(rows):
        for row in rows:
            for asset in row["assets"]:
                old = Path(asset["location"])
                target = dataset / "assets" / old.name
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    shutil.copy2(old, target)
                if file_hash(old) != file_hash(target):
                    raise ValueError("复制后图片哈希不一致")
                asset["location"] = str(target)

    original_representation = representation_hash(corpus)
    relocate(corpus)
    relocate(queries)
    if representation_hash(corpus) != original_representation:
        raise ValueError("派生版本改变了编码题面")
    request = {**read_json(parent / "request.json"), "query_count": 24}
    write_json(dataset / "request.json", request)
    manifest = {
        **read_json(parent / "manifest.json"),
        **request,
        "dataset_id": dataset.name,
        "status": "queries_ready",
        "label_status": "silver_pending",
        "purpose": "100题24查询流程验证；按正例可用性选族，不代表自然查询分布或大题库能力",
    }
    write_json(dataset / "manifest.json", manifest)
    write_rows(dataset / "corpus.jsonl", corpus)
    write_rows(dataset / "queries.jsonl", queries)
    selected_ids = {q["id"] for q in queries}
    selected_labels = [r for r in labels if r["query_id"] in selected_ids]
    write_rows(dataset / "qrels.jsonl", selected_labels)
    for name in (
        "models.json",
        "index.json",
        "excluded.jsonl",
        "private/references.json",
    ):
        source = parent / name
        if source.exists():
            target = dataset / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    for name in ("usage.jsonl", "encoding_usage.jsonl"):
        source = parent / "work" / name
        if source.exists():
            target = dataset / "work/inherited" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    write_json(
        dataset / "work/query_seeds.json",
        [
            {
                k: f[k]
                for k in ("source_id", "source_group", "intent", "input_kind", "split")
            }
            for f in audit
        ],
    )
    result = {
        "parent": str(parent),
        "corpus_size": len(corpus),
        "query_count": len(queries),
        "reused_queries": sum(q["id"] in reused_ids for q in queries),
        "reused_labels": len(selected_labels),
        "seed": SEED,
        "families": audit,
        "representation_hash": original_representation,
        "selection_basis": "仅按两条查询均有复判正例且候选池标注完整筛选；未读取排名或质量指标。新增查询只依据公开题面。",
        "cost_notice": "inherited为此前60查询设计及被替换查询的全部实际准备成本，不能当作24查询的独占成本。",
        "limitation": "重新按题族划分为开发12/测试12；父版本参与过标注诊断，非独立未接触测试集。",
    }
    write_json(dataset / "subset_selection.json", result)
    verify_assets(dataset, corpus)
    return result
