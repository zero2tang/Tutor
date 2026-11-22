"""用公开自拟题验证真实模型的答案语义复核，不访问个人学习档案。"""

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.infrastructure.clients.llm import ModelClient
from app.infrastructure.persistence.database import Base
from app.application.agent.runner import AgentRunner
from app.domains.practice.reviews import ACCEPTED_VERDICTS, ExerciseReviewService
from app.agent.tools.contracts import ToolContext
from app.application.agent.tools import TeachingTools


def samples():
    return [
        {
            "name": "分数与小数等价",
            "accepted": True,
            "draft": {
                "stem": "把一张纸平均分成四份，取其中两份。取出的部分占整张纸的多少？",
                "options": {},
                "question_type": "fill",
                "concept_ids": ["math:review_fixture"],
                "cognitive_type": "understanding",
                "answer": ["2/4"],
                "explanation": "四等份中取两份，比例为2/4。",
            },
            "independent": {
                "answer": ["0.5"],
                "explanation": "两份除以总共四份得到0.5。",
                "valid": True,
                "issue": "",
            },
        },
        {
            "name": "单位与推导说明互补",
            "accepted": True,
            "draft": {
                "stem": "一块长方形木板长六厘米、宽四厘米，求这块木板的面积。",
                "options": {},
                "question_type": "fill",
                "concept_ids": ["math:review_fixture"],
                "cognitive_type": "application",
                "answer": ["24平方厘米"],
                "explanation": "按长乘宽计算，6乘4得到24平方厘米。",
            },
            "independent": {
                "answer": ["0.0024平方米"],
                "explanation": "长为0.06米、宽为0.04米，面积为0.0024平方米，注意面积单位换算。",
                "valid": True,
                "issue": "",
            },
        },
        {
            "name": "实质计算冲突应拒绝",
            "accepted": False,
            "draft": {
                "stem": "小明原有两支铅笔，又买了三支，现在一共有多少支铅笔？",
                "options": {"A": "4支", "B": "5支", "C": "6支", "D": "7支"},
                "question_type": "choice",
                "concept_ids": ["math:review_fixture"],
                "cognitive_type": "recall",
                "answer": ["B"],
                "explanation": "2加3等于5，所以选择B。",
            },
            "independent": {
                "answer": ["C"],
                "explanation": "将2加3误算为6，所以选择C。",
                "valid": True,
                "issue": "",
            },
        },
        {
            "name": "正确部分结论互补",
            "accepted": True,
            "expected_verdict": "complementary",
            "draft": {
                "stem": "将三个苹果平均分给两个人，每人分得多少个苹果？请以整数部分加分数部分的形式填写完整结果。",
                "options": {},
                "question_type": "fill",
                "concept_ids": ["math:review_fixture"],
                "cognitive_type": "understanding",
                "answer": ["整数部分：1个"],
                "explanation": "这里只回答分配的整数部分：先给每人一个苹果，还剩一个苹果等待分配。",
            },
            "independent": {
                "answer": ["分数部分：1/2个"],
                "explanation": "这里只回答余下的分数部分：整数苹果分配后，余下一个苹果对半分，每人再得到半个。",
                "valid": True,
                "issue": "",
            },
        },
    ]


async def main():
    with TemporaryDirectory(prefix="stem-review-") as directory:
        engine = create_engine(
            "sqlite:///" + (Path(directory) / "review.db").as_posix()
        )
        from app.application.model_registry import register_models
        register_models()
        Base.metadata.create_all(engine)

        @contextmanager
        def database():
            with Session(engine, expire_on_commit=False) as db:
                with db.begin():
                    yield db

        models = ModelClient()
        service = ExerciseReviewService(
            AgentRunner(models, TeachingTools(SimpleNamespace()))
        )

        async def check(sample):
            try:
                result = await service.reconcile(
                    sample["draft"],
                    sample["independent"],
                    ToolContext(course_id="math"),
                    "semantic-review-evaluation",
                    None,
                )
                output = result["output"]
                return {
                    "name": sample["name"],
                    "expected_accept": sample["accepted"],
                    "passed": (output["verdict"] in ACCEPTED_VERDICTS)
                    == sample["accepted"]
                    and (
                        not sample.get("expected_verdict")
                        or output["verdict"] == sample["expected_verdict"]
                    ),
                    "output": output,
                    "usage": result["usage"],
                    "duration_ms": result["duration_ms"],
                }
            except Exception as exc:
                return {
                    "name": sample["name"],
                    "passed": False,
                    "error": type(exc).__name__ + ": " + str(exc)[:300],
                }

        try:
            with patch("app.application.agent.runner.db_session", database):
                results = await asyncio.gather(*(check(sample) for sample in samples()))
        finally:
            await models.close()
            engine.dispose()
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": models.settings.llm_model,
        "personal_records_read_or_written": False,
        "passed": sum(item["passed"] for item in results),
        "total": len(results),
        "cases": results,
    }
    path = ROOT / "data/reports/exercise_review_live.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "total": report["total"],
                "verdicts": [
                    (item.get("output") or {}).get("verdict") for item in results
                ],
                "report": str(path),
            },
            ensure_ascii=True,
        )
    )
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
