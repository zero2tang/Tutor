"""退役流程的固定历史投影；在线请求不再查询或重新统计旧表。"""

import json
from functools import lru_cache
from pathlib import Path

ARCHIVE = Path(__file__).resolve().parents[4] / "data/archives/simplification_p0/20261023/student_summaries.json"


@lru_cache(maxsize=1)
def _summaries():
    if not ARCHIVE.exists():
        return {}
    return json.loads(ARCHIVE.read_text(encoding="utf-8"))["students"]


def archived_summary(student_id: str) -> dict | None:
    """缺少历史档案时不制造零分或已评估状态。"""
    return _summaries().get(student_id)
