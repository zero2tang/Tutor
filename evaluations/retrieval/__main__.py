"""python -m evaluations.retrieval <阶段>；所有状态写入独立评测目录。"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from .common.config import DEFAULT_DATASET, HOME
from .common.files import write_json, read_json, append_row


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "stage",
        choices=[
            "prepare",
            "index",
            "pool",
            "label",
            "repair",
            "freeze",
            "run",
            "report",
            "verify",
            "review",
            "status",
            "subset",
        ],
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--size", type=int, default=100)
    parser.add_argument("--queries", type=int, default=24)
    parser.add_argument("--parent", type=Path, default=HOME / "datasets/math_100_v1")
    parser.add_argument(
        "--selection-plan", type=Path, default=HOME / "datasets/subset_24_plan.json"
    )
    parser.add_argument("--snapshot-only", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--smoke", action="store_true", help="pool阶段每个意图/输入模态选择一条查询"
    )
    parser.add_argument("--run-id", default="math_100_q24_v1_initial")
    parser.add_argument("--experiments", help="例如 E1,E2；省略运行全部组")
    parser.add_argument("--no-timing", action="store_true")
    parser.add_argument("--no-ragas", action="store_true")
    parser.add_argument("--ragas-python", type=Path)
    args = parser.parse_args()
    try:
        if args.stage == "subset":
            from .common.subsetting import derive_subset

            result = derive_subset(args.parent, args.dataset, args.selection_plan)
        elif args.stage == "prepare":
            from .common.dataset import prepare_snapshot
            from .common.queries import enrich

            result = prepare_snapshot(args.dataset, args.size, args.queries)
            if not args.snapshot_only:
                asyncio.run(enrich(args.dataset, limit=args.limit))
                result = read_json(args.dataset / "manifest.json")
        elif args.stage == "index":
            from .common.retrievers import build_index

            result = asyncio.run(build_index(args.dataset, limit=args.limit))
        elif args.stage == "pool":
            from .common.pooling import collect_pool

            result = asyncio.run(
                collect_pool(args.dataset, limit=args.limit, smoke=args.smoke)
            )
        elif args.stage == "label":
            from .common.labeling import label_pool

            result = asyncio.run(label_pool(args.dataset, limit=args.limit))
        elif args.stage == "freeze":
            from .common.freezing import freeze

            result = freeze(args.dataset)
        elif args.stage == "repair":
            from .common.replacement import repair_missing_positives

            result = repair_missing_positives(args.dataset)
        elif args.stage == "status":
            from .common.readiness import status_report

            result = status_report(args.dataset)
        elif args.stage == "run":
            from .common.running import run

            result = asyncio.run(
                run(
                    args.dataset,
                    args.run_id,
                    limit=args.limit,
                    experiment_ids=(
                        args.experiments.split(",") if args.experiments else None
                    ),
                    timing=not args.no_timing,
                )
            )
        elif args.stage == "report":
            from .common.reporting import report

            result = report(
                args.run_id, ragas_python=args.ragas_python, use_ragas=not args.no_ragas
            )
        elif args.stage == "verify":
            from .common.dataset import validate_dataset

            result = validate_dataset(args.dataset, require_labels=True)
        else:
            from .common.reporting import review_package

            result = review_package(args.dataset)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        append_row(
            args.dataset / "work/stage_events.jsonl",
            {"stage": args.stage, "status": "completed"},
        )
    except Exception as exc:
        value = {
            "stage": args.stage,
            "status": "failed",
            "error": type(exc).__name__,
            "message": str(exc)[:500],
        }
        write_json(args.dataset / "work" / (args.stage + "-last-error.json"), value)
        append_row(args.dataset / "work/stage_events.jsonl", value)
        print(json.dumps(value, ensure_ascii=False), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
