"""在隔离虚拟环境内运行RAGAS，不导入生产服务或调用裁判模型。"""

import argparse
import asyncio
import json
import math
from pathlib import Path


async def score(rows):
    from ragas import SingleTurnSample
    from ragas.metrics import IDBasedContextPrecision, IDBasedContextRecall

    precision, recall = IDBasedContextPrecision(), IDBasedContextRecall()
    output = []
    for row in rows:
        retrieved = list(dict.fromkeys(row["retrieved"]))
        reference = list(dict.fromkeys(row["reference"]))
        if not reference:
            raise ValueError("没有正例的查询不能计算召回率")
        sample = SingleTurnSample(
            retrieved_context_ids=retrieved, reference_context_ids=reference
        )
        p, r = await precision.single_turn_ascore(
            sample
        ), await recall.single_turn_ascore(sample)
        output.append(
            {
                "key": row["key"],
                "precision": p if math.isfinite(p) else None,
                "recall": r if math.isfinite(r) else None,
            }
        )
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = json.loads(args.input.read_text(encoding="utf-8"))
    args.output.write_text(
        json.dumps(asyncio.run(score(rows)), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
