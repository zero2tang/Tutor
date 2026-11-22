"""用真实隔离RAGAS包核对集合公式及空返回边界。"""

import pytest
from evaluations.retrieval.common.config import HOME
from evaluations.retrieval.common.metrics import query_metrics
from evaluations.retrieval.common.ragas_adapter import compare


def test_real_ragas_id_metrics(tmp_path):
    if not (HOME / ".venv-ragas/Scripts/python.exe").exists():
        pytest.skip("未安装隔离RAGAS环境，不能冒充集成验证")
    labels = {"a": 3, "b": 2, "x": 0}
    rows = [
        {
            "experiment": "fixture",
            "query_id": str(i),
            "result": {"rankings": [{"id": x} for x in ids]},
            "metrics": query_metrics(ids, labels),
        }
        for i, ids in enumerate((["a", "x"], ["x"], []))
    ]
    qrels = [
        {"query_id": str(i), "question_id": cid, "grade": grade}
        for i in range(3)
        for cid, grade in labels.items()
    ]
    result = compare(rows, qrels, tmp_path)
    assert result["status"] == "passed", result
    assert result["comparisons"] == 9
