"""修复课程历史派生投影，保留真实作答事实。"""

def repair_umbrella_projection() -> None:
    """课程总标签不是单道题能验证的技能，保留原始作答并修正其派生投影。"""
    from sqlalchemy import select
    from app.infrastructure.persistence.database import db_session
    from app.domains.courses.models import IndexTask
    from app.domains.students.models import StudentState

    with db_session() as db:
        identifier = "student_projection:umbrella:v1"
        if db.get(IndexTask, identifier):
            return
        affected = []
        for state in db.scalars(
            select(StudentState).where(StudentState.concept_id == "mechanics:c198")
        ):
            affected.append(
                {
                    "id": state.id,
                    "previous_mastery": state.mastery,
                    "original_evidence_ids": state.evidence_ids,
                }
            )
            state.mastery = {"beginner": None, "advanced": None, "proficient": None}
            state.evidence_ids, state.evidence_questions, state.misconceptions = (
                [],
                [],
                [],
            )
            state.next_review_at = None
            state.version += 1
        db.add(
            IndexTask(
                id=identifier,
                target="student_projection",
                status="ready",
                details={
                    "repair": "排除课程总标签的单题诊断",
                    "affected": affected,
                    "original_attempts_preserved": True,
                },
            )
        )
