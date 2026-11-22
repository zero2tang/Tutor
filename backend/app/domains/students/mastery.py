"""学生掌握状态的证据归约；消费冻结账本投影，不回调评分服务。"""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

from app.domains.students.models import StudentState

LEVELS = ["beginner", "advanced", "proficient"]


@dataclass(frozen=True)
class MasteryEvidence:
    """评分域提供的只读输入；不包含可写的 ORM 引用或后续诊断提案。"""

    source_ref: str
    evidence_group_id: str
    canonical_item_id: str
    dimension: str
    created_at: datetime
    eligible_for_mastery: bool
    score: float | None
    legacy_baseline: dict | None


def apply_ledger_evidence(
    db,
    *,
    evidence_by_concept: Iterable[tuple[str, Sequence[MasteryEvidence]]],
    student_id: str,
    attempt_id: str,
) -> list[dict]:
    """在调用方事务中重算学生状态；重评替换旧贡献，同题同维度只计一次。"""
    changes = []
    # 输入已按知识点排序；逐组读取和归约，保留原事务中读账本与锁状态的交错。
    for cid, links in evidence_by_concept:
        if not links:
            continue
        identifier = student_id + ":" + cid
        state = db.get(StudentState, identifier, with_for_update=True)
        baseline = deepcopy(links[0].legacy_baseline or {
            "mastery": dict.fromkeys(LEVELS), "evidence_ids": [],
            "evidence_questions": [], "misconceptions": [],
        })
        qualifying = [link for link in links if link.eligible_for_mastery]
        if state is None and not qualifying:
            continue
        before = dict(state.mastery) if state else dict.fromkeys(LEVELS)
        old_ids = list(state.evidence_ids) if state else []
        old_questions = list(state.evidence_questions) if state else []
        if state is None:
            state = StudentState(id=identifier, student_id=student_id, concept_id=cid,
                mastery=dict.fromkeys(LEVELS), evidence_ids=[], evidence_questions=[], misconceptions=[], version=0)
            db.add(state)
        mastery = dict(baseline["mastery"])
        evidence_ids, evidence_questions = list(baseline["evidence_ids"]), list(baseline["evidence_questions"])
        by_event = {}
        for link in qualifying:
            by_event.setdefault((link.evidence_group_id, link.dimension), []).append(link)
        used = set()
        dimension_counts = {level: len(baseline["evidence_questions"]) if mastery.get(level) is not None else 0 for level in LEVELS}
        event_order = {}
        for link in links:
            event_order.setdefault(link.evidence_group_id, link.created_at)
        for (_event, dimension), group in sorted(by_event.items(), key=lambda pair: (event_order[pair[0][0]], pair[0])):
            first = group[0]
            key = (first.canonical_item_id, dimension)
            if key in used or first.canonical_item_id in baseline["evidence_questions"]:
                continue
            used.add(key)
            dimension_counts[dimension] += 1
            scores = [float(link.score) for link in group if link.score is not None]
            if not scores:
                continue
            score = sum(scores) / len(scores)
            result = "correct" if score == 1 else "incorrect" if score == 0 else "partial"
            current = mastery.get(dimension)
            if current is None:
                current = 0.5 if result == "correct" else 0.25 if result == "partial" else 0.0
            else:
                # 增量来自冻结局部结果，不读取后续诊断模型的suggested_delta。
                current = max(0.0, min(1.0, current + (0.15 if result == "correct" else -0.15 if result == "incorrect" else 0.0)))
            count = dimension_counts[dimension]
            mastery[dimension] = round(min(current, 0.5 if count == 1 else 0.8 if count == 2 else 1.0), 3)
            aid = first.source_ref.removeprefix("attempt:")
            if aid not in evidence_ids:
                evidence_ids.append(aid)
            if first.canonical_item_id not in evidence_questions:
                evidence_questions.append(first.canonical_item_id)
        if before == mastery and old_ids == evidence_ids and old_questions == evidence_questions:
            continue
        state.mastery, state.evidence_ids, state.evidence_questions = mastery, evidence_ids, evidence_questions
        state.misconceptions = baseline.get("misconceptions", [])
        state.version += 1
        state.next_review_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
        changes.append({"concept_id": cid, "before": before, "after": mastery, "evidence_id": attempt_id,
            "version": state.version, "update_kind": "initialization" if all(v is None for v in before.values()) else "ledger_recompute",
            "applied_delta": round(sum((mastery.get(key) or 0) - (before.get(key) or 0) for key in LEVELS), 3)})
    return changes
