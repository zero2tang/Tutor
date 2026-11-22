"""补齐试点课程目录，不生成题目与作答证据。"""

from app.infrastructure.persistence.database import db_session
from app.domains.courses.models import Concept, Course

COURSE_ID = "junior_algebra"
CONCEPTS = {
    "signed_multiplication": "有理数乘法",
    "distribution": "去括号与分配律",
    "equivalence": "等式的等价变形",
    "linear_equation": "一元一次方程求解",
}


def seed_course() -> None:
    """仅补齐初中试点课程与知识目录，不生成题目、答案或计算校验数据。"""
    with db_session() as db:
        if db.get(Course, COURSE_ID) is None:
            db.add(
                Course(
                    id=COURSE_ID,
                    title="初中数学 · 一元一次方程",
                    description="通过通用讲题 Skill 阅读资料、逐步引导，并记录真实学习过程。",
                    source_ref="curriculum://junior-algebra",
                )
            )
        for identifier, title in CONCEPTS.items():
            concept_id = f"{COURSE_ID}:{identifier}"
            if db.get(Concept, concept_id) is None:
                db.add(
                    Concept(
                        id=concept_id,
                        course_id=COURSE_ID,
                        source_id=identifier,
                        name=title,
                        module="一元一次方程",
                        topic_type="procedure",
                        summary=f"用于组织{title}的教学资料与作答观察；不由单题推断长期掌握。",
                    )
                )
