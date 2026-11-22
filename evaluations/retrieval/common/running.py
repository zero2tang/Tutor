"""质量重放与独立计时；每个查询实验有独立的恢复记录。"""

import time
from pathlib import Path
from .config import HOME, experiments, experiment_hash, implementation_snapshot
from .files import verify_freeze, read_rows, read_json, write_json, pin, append_row
from .retrievers import Retriever
from .metrics import query_metrics


async def run(dataset, run_id, *, limit=None, experiment_ids=None, timing=True):
    dataset = Path(dataset)
    frozen = verify_freeze(dataset)
    if frozen["experiment_hash"] != experiment_hash():
        raise ValueError("实验配置已变化；请发布新基准版本并重新建池标注")
    if not run_id or any(
        c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        for c in run_id
    ):
        raise ValueError("run-id只能包含字母数字、横线和下划线")
    destination = HOME / "runs" / run_id
    config = {
        "dataset": str(dataset.resolve()),
        "dataset_hash": frozen["dataset_hash"],
        "experiment_hash": experiment_hash(),
        "implementation": implementation_snapshot(),
        "timing": (
            "one_warmup_three_rounds_precomputed_vectors" if timing else "disabled"
        ),
    }
    pin(destination / "run.json", config)
    selected = [
        e for e in experiments() if not experiment_ids or e.id in experiment_ids
    ]
    if experiment_ids and set(experiment_ids) - {e.id for e in selected}:
        raise ValueError("未知实验编号")
    queries = read_rows(dataset / "queries.jsonl")
    judgments = read_rows(dataset / "qrels.jsonl")
    retriever = Retriever(dataset)
    try:
        retriever.require_index()
        for position, q in enumerate(queries[:limit] if limit else queries):
            labels = {
                r["question_id"]: r["grade"]
                for r in judgments
                if r["query_id"] == q["id"]
            }
            hard = {
                r["question_id"]
                for r in judgments
                if r["query_id"] == q["id"] and r["near_miss"]
            }
            for e in selected:
                path = destination / e.id / (q["id"] + ".json")
                old = read_json(path)
                if old and old.get("status") == "completed":
                    continue
                record = {
                    "query_id": q["id"],
                    "experiment": e.id,
                    "source_group": q["source_group"],
                    "intent": q["intent"],
                    "input_kind": q["input_kind"],
                    "split": q["split"],
                    "module": q["module"],
                }
                try:
                    vectors = await retriever.vectors(q, e.channels)
                    result = await retriever.retrieve(q, e, vectors)
                    record["result"] = result
                    ids = [r["id"] for r in result["rankings"]]
                    metrics = query_metrics(ids, labels, hard)
                    if not metrics["label_complete"]:
                        raise ValueError("新排名包含未标注题，必须升级标签版本后重跑")
                    record.update(
                        status="completed",
                        result=result,
                        metrics=metrics,
                        retrieved_has_image={
                            r: retriever.lookup[r]["has_image"] for r in ids[:20]
                        },
                    )
                except Exception as exc:
                    record.update(
                        status="failed",
                        error=type(exc).__name__,
                        message=str(exc)[:250],
                    )
                write_json(path, record)
                append_row(
                    destination / "attempts.jsonl",
                    {
                        k: record[k]
                        for k in (
                            "query_id",
                            "experiment",
                            "status",
                            "error",
                            "message",
                        )
                        if k in record
                    },
                )
                print(f"run {e.id} {q['id']} {record['status']}", flush=True)
            if timing:
                timing_path = destination / "timing" / (q["id"] + ".json")
                previous = read_json(timing_path, {})
                if (
                    previous.get("experiments") == [e.id for e in selected]
                    and previous.get("status") == "completed"
                ):
                    continue
                try:
                    vectors = await retriever.vectors(
                        q, {channel for e in selected for channel in e.channels}
                    )
                    for e in selected:
                        await retriever.retrieve(q, e, vectors)
                    observations = []
                    for round_id in range(3):
                        shift = (position + round_id) % len(selected)
                        for e in selected[shift:] + selected[:shift]:
                            result = await retriever.retrieve(q, e, vectors)
                            observations.append(
                                {
                                    "experiment": e.id,
                                    "round": round_id,
                                    "ms": result["retrieval_ms"],
                                }
                            )
                    write_json(
                        timing_path,
                        {
                            "status": "completed",
                            "experiments": [e.id for e in selected],
                            "observations": observations,
                        },
                    )
                except Exception as exc:
                    write_json(
                        timing_path, {"status": "failed", "error": type(exc).__name__}
                    )
        return {"run_id": run_id, "path": str(destination)}
    finally:
        await retriever.close()
