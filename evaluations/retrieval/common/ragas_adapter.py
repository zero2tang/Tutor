"""RAGAS仅校验ID集合指标，与固定k分母的Precision分开保存。"""

import os
import subprocess
from pathlib import Path
from .config import HOME
from .files import write_json, read_json


def compare(rows, qrels, output_dir, python=None):
    python = Path(python) if python else HOME / ".venv-ragas/Scripts/python.exe"
    if not python.exists():
        return {
            "status": "unavailable",
            "reason": "隔离RAGAS环境尚未安装，未用本地公式冒充RAGAS输出",
        }
    references = {}
    for row in qrels:
        if row["grade"] is not None and row["grade"] >= 2:
            references.setdefault(row["query_id"], []).append(row["question_id"])
    payload = []
    expected = {}
    for row in rows:
        for k in (5, 10, 20):
            key = f"{row['experiment']}:{row['query_id']}:{k}"
            ids = [r["id"] for r in row["result"]["rankings"][:k]]
            payload.append(
                {"key": key, "retrieved": ids, "reference": references[row["query_id"]]}
            )
            expected[key] = (
                row["metrics"][f"id_precision_at_{k}"] if ids else None,
                row["metrics"][f"recall_at_{k}"],
            )
    source, target = (
        Path(output_dir) / "ragas-input.json",
        Path(output_dir) / "ragas-output.json",
    )
    write_json(source, payload)
    env = {**os.environ, "RAGAS_DO_NOT_TRACK": "true"}
    try:
        result = subprocess.run(
            [
                str(python),
                str(HOME / "common/ragas_worker.py"),
                "--input",
                str(source),
                "--output",
                str(target),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {"status": "failed", "reason": "RAGAS指标进程超时"}
    if result.returncode:
        return {"status": "failed", "reason": result.stderr[-1500:]}
    scores = read_json(target)

    def same(a, b):
        return a is b if a is None or b is None else abs(a - b) <= 1e-8

    mismatches = [
        r["key"]
        for r in scores
        if r["key"] not in expected
        or not same(r["precision"], expected[r["key"]][0])
        or not same(r["recall"], expected[r["key"]][1])
    ]
    return {
        "status": (
            "passed" if not mismatches and len(scores) == len(expected) else "mismatch"
        ),
        "ragas_version": "0.4.3",
        "comparisons": len(scores),
        "mismatches": mismatches,
        "precision_denominator": "实际返回数；主表Precision@k分母固定为k",
        "empty_list_notice": "RAGAS对空列表ID Precision返回NaN，JSON记录null；本地指标约定为0，单独核对边界，不宣称数值相等",
    }
