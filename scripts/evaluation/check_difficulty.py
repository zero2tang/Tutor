"""离线核验题目单一难度来源；无模型调用、不修改数据库。"""
import json
from pathlib import Path
import sys
from collections import Counter
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT / "backend"))
from sqlalchemy import select
from app.infrastructure.persistence.database import db_session
from app.domains.courses.models import Question
from app.domains.courses.difficulty.metadata import estimate


def main():
    counts = Counter()
    with db_session() as db:
        for question in db.scalars(select(Question)):
            value = estimate(question)
            counts[question.course_id+":"+value.get("status","unknown")] += 1
            assert value.get("value") is None or 0 <= value["value"] <= 100
    print(json.dumps({"single_question_source":True,"statuses":dict(counts)},ensure_ascii=False))

if __name__ == "__main__":
    main()
