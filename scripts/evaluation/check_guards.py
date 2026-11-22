"""检查缺图题的公开状态、接口拦截及课程总标签的评估边界。"""

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import select
from app.infrastructure.persistence.database import db_session
from app.domains.courses.models import Question
from app.domains.courses.catalog import public_question


def main() -> None:
    report = json.loads(
        (ROOT / "data/reports/mechanics_acceptance.json").read_text(encoding="utf-8")
    )
    with db_session() as db:
        originals = [
            public_question(q)
            for q in db.scalars(
                select(Question).where(
                    Question.course_id == "mechanics", Question.generated == False
                )
            )
        ]
    missing = [q["id"] for q in originals if q["availability"] == "missing_figure"]
    assert len(originals) == 177 and len(missing) == 3
    checks = []
    with httpx.Client(
        base_url="http://127.0.0.1:8008/api", timeout=30, trust_env=False
    ) as client:
        session = (
            client.post(
                "/sessions",
                json={
                    "goal": "缺图题接口边界验收",
                    "concept_ids": ["mechanics:c187"],
                    "student_id": "evaluation_" + uuid4().hex[:12],
                },
            )
            .raise_for_status()
            .json()
        )
        for qid in missing:
            question = (
                client.get("/search/questions", params={"q": qid, "mode": "keyword"})
                .raise_for_status()
                .json()["items"][0]["question"]
            )
            assert (
                question["availability"] == "missing_figure"
                and "answer" not in question
            )
            response = client.post(
                f"/sessions/{session['id']}/practice?stream=false",
                json={"question_id": qid, "cognitive_type": "recall", "request_id": str(uuid4())},
            )
            assert response.status_code == 400 and "图" in response.json()["detail"]
            checks.append(
                {
                    "question_id": qid,
                    "availability": question["availability"],
                    "http_status": response.status_code,
                    "reason": response.json()["detail"],
                }
            )
        recovered = client.get(f"/sessions/{session['id']}").raise_for_status().json()
        assert not recovered["active_turn_id"] and not recovered["pending_question"]
        profile = (
            client.get(
                "/student", params={"student_id": report["artifacts"]["student_id"]}
            )
            .raise_for_status()
            .json()
        )
        umbrella = next(
            state
            for state in profile["states"]
            if state["concept_id"] == "mechanics:c198"
        )
        assert umbrella["status"] == "unassessed" and all(
            v is None for v in umbrella["mastery"].values()
        )
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "passed",
        "original_questions": len(originals),
        "available_original_questions": len(originals) - len(missing),
        "missing_figure_checks": checks,
        "failed_requests_release_session": True,
        "umbrella_concept": umbrella,
    }
    (ROOT / "data/reports/runtime_guards.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
