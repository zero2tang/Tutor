"""查询回执使用有界本地诊断日志；日志故障不影响检索结果。"""

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

logger = logging.getLogger("tutor.retrieval")


def record(trace_id, course_id, session_id, payload):
    try:
        if not logger.handlers:
            root = Path(__file__).resolve().parents[4] / "data/logs"
            root.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(root / "retrieval.jsonl", maxBytes=8*1024*1024,
                                          backupCount=2, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            logger.propagate = False
        logger.info(json.dumps({"trace_id": trace_id, "course_id": course_id,
                               "session_id": session_id, "receipt": payload}, ensure_ascii=False))
    except Exception:
        # 诊断是尽力记录，正式检索结果继续返回。
        pass
