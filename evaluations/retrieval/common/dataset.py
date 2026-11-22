"""从本地公开题库分层取样，答案仅进入独立的标注参考文件。"""

from collections import Counter, defaultdict
import math
import random
import re
import shutil
from types import SimpleNamespace
from pathlib import Path

from app.application.commands.courses.import_math import DEFAULT_SOURCE, prepare_course
from app.domains.courses.questions.search.representation import (
    public_snapshot,
    math_assets,
    source_hash,
    duplicate_key,
    retrieval_text,
    clean_text,
)
from app.infrastructure.documents.question_images import QuestionImageReader
from .files import (
    digest,
    read_json,
    read_rows,
    write_json,
    write_rows,
    pin,
    mutable,
    file_hash,
)
from .config import SEED, INTENTS, experiment_hash


def template_key(text, module):
    # 数字变体归入同族；保守合并会减少可用题族，不会为凑配额拆分同族。
    normalized = re.sub(r"\d+(?:\.\d+)?", "#", clean_text(text))
    normalized = re.sub(r"\s+|\$|[，。；、,.!?！？]", "", normalized)
    return digest([module, normalized])


def proportional_sample(rows, total, rng):
    buckets = defaultdict(list)
    for row in rows:
        buckets[(row["module"], row["question_type"])].append(row)
    if len(rows) < total:
        raise ValueError(f"可用题只有{len(rows)}，不足{total}；拒绝伪造或重复补足")
    ordered = sorted(buckets)
    quotas = {key: math.floor(total * len(buckets[key]) / len(rows)) for key in ordered}
    remainder = sorted(
        ordered, key=lambda k: (-(total * len(buckets[k]) / len(rows) - quotas[k]), k)
    )
    for key in remainder[: total - sum(quotas.values())]:
        quotas[key] += 1
    selected = []
    for key in ordered:
        candidates = sorted(buckets[key], key=lambda row: row["id"])
        rng.shuffle(candidates)
        selected.extend(candidates[: quotas[key]])
    return selected


def prepare_snapshot(dataset, size=100, query_count=24):
    dataset = Path(dataset)
    mutable(dataset)
    if size < 12 or query_count < 24 or size % 2 or query_count % 12:
        raise ValueError("题库规模必须为偶数；查询规模必须是12的倍数")
    sources = {
        name: file_hash(DEFAULT_SOURCE / name)
        for name in ("questions.json", "kc_routes_map.json")
    }
    request = {
        "size": size,
        "query_count": query_count,
        "seed": SEED,
        "sources": sources,
        "schema": "math-retrieval-v1",
        "experiment_hash": experiment_hash(),
    }
    pin(dataset / "request.json", request)
    if (dataset / "manifest.json").exists():
        verify_assets(dataset, read_rows(dataset / "corpus.jsonl"))
        return read_json(dataset / "manifest.json")
    reader, rng = QuestionImageReader(), random.Random(SEED)
    bundle = prepare_course()
    rows, excluded, private, seen = [], [], {}, set()
    for q in bundle["questions"]:
        if q["validation"]["availability"] != "ready":
            excluded.append({"id": q["id"], "reason": q["validation"]["availability"]})
            continue
        snapshot = public_snapshot(SimpleNamespace(version=1, **q))
        assets = math_assets(snapshot, reader)
        if any(a["status"] != "ready" for a in assets) or len(assets) > 10:
            excluded.append(
                {"id": q["id"], "reason": "unreadable_or_over_budget_assets"}
            )
            continue
        text = retrieval_text(snapshot)
        if not text.strip() and not assets:
            excluded.append({"id": q["id"], "reason": "empty_public_content"})
            continue
        if len(text) > 20000:
            excluded.append({"id": q["id"], "reason": "fusion_text_budget"})
            continue
        identity = duplicate_key(snapshot, assets)
        if identity in seen:
            excluded.append({"id": q["id"], "reason": "duplicate_content"})
            continue
        seen.add(identity)
        module = " / ".join(q["validation"]["knowledge_routes"][0].split("----")[:2])
        family = template_key(text, module)
        rows.append(
            {
                "id": q["id"],
                "snapshot": snapshot,
                "native_text": text,
                "text": text,
                "source_hash": source_hash(snapshot, assets),
                "duplicate_key": identity,
                "source_group": family,
                "module": module,
                "question_type": q["question_type"],
                "has_image": bool(assets),
                "assets": assets,
                "transcription_status": "pending" if assets else "native_only",
            }
        )
        private[q["id"]] = {"answer": q["answer"], "explanation": q["explanation"]}
    selected = []
    for image in (False, True):
        selected.extend(
            proportional_sample(
                [r for r in rows if r["has_image"] == image], size // 2, rng
            )
        )
    selected.sort(key=lambda r: r["id"])
    # 图片按内容哈希复制到评测目录，快照不依赖以后被替换的原题文件。
    for row in selected:
        for asset in row["assets"]:
            source = Path(asset["location"])
            target = (
                dataset / "assets" / (asset["content_hash"] + source.suffix.lower())
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copyfile(source, target)
            asset["location"] = str(target.resolve())
    write_rows(dataset / "corpus.jsonl", selected)
    write_rows(dataset / "excluded.jsonl", excluded)
    write_json(
        dataset / "private/references.json",
        {r["id"]: private[r["id"]] for r in selected},
    )
    manifest = {
        **request,
        "dataset_id": dataset.name,
        "status": "snapshot_ready",
        "label_status": "silver_pending",
        "actual_size": len(selected),
        "image_counts": dict(
            Counter("image" if r["has_image"] else "text" for r in selected)
        ),
        "strata": dict(
            Counter(
                r["module"] + "|" + r["question_type"] + "|" + str(r["has_image"])
                for r in selected
            )
        ),
        "excluded_count": len(excluded),
        "available_source_count": len(rows),
        "purpose": "均衡能力诊断，不代表线上自然流量；同构和同方法只报告候选池召回率",
        "family_policy": "相同规范题面及数字模板保守合并；近重复关系由复核包继续检查，不声称语义近重复识别完备",
    }
    write_json(dataset / "manifest.json", manifest)
    write_rows(dataset / "queries.jsonl", [])
    write_rows(dataset / "qrels.jsonl", [])
    return manifest


def verify_assets(dataset, corpus):
    reader = QuestionImageReader(roots=[Path(dataset) / "assets"])
    for row in corpus:
        for asset in row["assets"]:
            reader.read(asset["location"], asset["content_hash"], with_identity=False)


def query_seeds(dataset):
    path = Path(dataset) / "work/query_seeds.json"
    existing = read_json(path)
    if existing is not None:
        return existing
    manifest = read_json(Path(dataset) / "manifest.json")
    corpus = read_rows(Path(dataset) / "corpus.jsonl")
    rng, used, output = random.Random(SEED), set(), []
    count = manifest["query_count"] // 12
    dev_families = max(1, round(count * 0.2))
    for modality in ("text", "image"):
        rows = [r for r in corpus if r["has_image"] == (modality == "image")]
        rng.shuffle(rows)
        for intent in INTENTS:
            chosen = []
            for row in rows:
                if row["source_group"] in used:
                    continue
                used.add(row["source_group"])
                chosen.append(row)
                if len(chosen) == count:
                    break
            if len(chosen) < count:
                raise ValueError(f"{intent}/{modality}独立题族不足，拒绝拆分同族")
            for i, row in enumerate(chosen):
                output.append(
                    {
                        "source_id": row["id"],
                        "source_group": row["source_group"],
                        "intent": intent,
                        "input_kind": modality,
                        "split": "development" if i < dev_families else "test",
                    }
                )
    write_json(path, output)
    return output


def validate_text(text):
    if not isinstance(text, str) or not text.strip() or len(text) > 20000:
        raise ValueError("文本为空或超过统一图文预算")
    if re.search(
        r"https?://|/api/assets/|(?:math|mechanics):\w+|(?:question|analysis)_\d+-image|[A-Za-z]:[\\/]",
        text,
    ):
        raise ValueError("编码文本包含路径、来源题号或资源标识")


def validate_dataset(dataset, require_labels=False):
    manifest = read_json(Path(dataset) / "manifest.json")
    corpus = read_rows(Path(dataset) / "corpus.jsonl")
    queries = read_rows(Path(dataset) / "queries.jsonl")
    problems = []
    ids = {r["id"] for r in corpus}
    lookup = {r["id"]: r for r in corpus}
    if len(ids) != manifest["size"] or len(corpus) != len(ids):
        problems.append("题库数量或唯一性不满足配额")
    if sum(r["has_image"] for r in corpus) != manifest["size"] // 2:
        problems.append("题库图文配额不一致")
    if any(r["transcription_status"] == "pending" for r in corpus):
        problems.append("题图转录尚未完成")
    counts, splits, families = Counter(), {}, defaultdict(list)
    for row in corpus:
        validate_text(row["text"])
    for q in queries:
        validate_text(q["text"])
        counts[(q["intent"], q["input_kind"])] += 1
        families[q["source_group"]].append(q)
        if q["source_group"] in splits and splits[q["source_group"]] != q["split"]:
            problems.append("同源题族跨集合")
        splits[q["source_group"]] = q["split"]
        if q["source_id"] not in ids:
            problems.append("查询来源不属于候选库")
        else:
            source = lookup[q["source_id"]]
            if q["source_group"] != source["source_group"]:
                problems.append("查询题族与来源不匹配")
            if bool(q["assets"]) != (q["input_kind"] == "image"):
                problems.append("查询输入模态与实际图片不匹配")
            if q["intent"] != "exact" and any(
                r["id"] not in q["excluded_ids"]
                for r in corpus
                if r["duplicate_key"] == source["duplicate_key"]
            ):
                problems.append("同构或同方法查询未排除源题重复内容")
            if q["intent"] == "exact" and q["known_targets"] != [source["id"]]:
                problems.append("原题查询已知目标不一致")
    if len(queries) != manifest["query_count"] or len(
        {q["id"] for q in queries}
    ) != len(queries):
        problems.append("查询数量或唯一性不满足配额")
    if any(
        counts[(i, m)] != manifest["query_count"] // 6
        for i in INTENTS
        for m in ("text", "image")
    ):
        problems.append("查询六个分层配额不一致")
    if any(len(group) != 2 for group in families.values()):
        problems.append("每来源题族必须恰好两条查询")
    dev_per_cell = max(1, round(manifest["query_count"] // 12 * 0.2)) * 2
    split_counts = Counter((q["intent"], q["input_kind"], q["split"]) for q in queries)
    for intent in INTENTS:
        for modality in ("text", "image"):
            if (
                split_counts[intent, modality, "development"] != dev_per_cell
                or split_counts[intent, modality, "test"]
                != manifest["query_count"] // 6 - dev_per_cell
            ):
                problems.append("开发/测试分层配额不一致")
    if require_labels:
        qrels = read_rows(Path(dataset) / "qrels.jsonl")
        for query in queries:
            rows = [r for r in qrels if r["query_id"] == query["id"]]
            if not any(r["grade"] is not None and r["grade"] >= 2 for r in rows):
                problems.append("缺少已确认正例：" + query["id"])
            if any(
                r["grade"] is not None
                and r["grade"] >= 2
                and r["question_id"] in query["excluded_ids"]
                for r in rows
            ):
                problems.append("排除源题被误计入正例")
    return {
        "valid": not problems,
        "problems": list(dict.fromkeys(problems)),
        "corpus": len(corpus),
        "queries": len(queries),
        "source_groups": len(families),
        "query_strata": {"/".join(k): v for k, v in counts.items()},
    }
