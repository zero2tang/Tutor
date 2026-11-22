"""冻结逐知识点评分，保存归因，并按原始事件与实质题目去重。"""

from copy import deepcopy
from hashlib import sha256
import json
import re
import unicodedata
from typing import Iterator

from sqlalchemy import select

from app.domains.assessment.models import EvidenceConceptLink
from app.domains.students.models import StudentState
from app.domains.students.mastery import MasteryEvidence
from app.domains.assessment.grading_schemas import GradingPacket

LEVELS = ["beginner", "advanced", "proficient"]
RESULTS = {"correct", "partial", "incorrect"}


def _consistent_score(result, score):
    return isinstance(score, (int, float)) and (
        (result == "correct" and score == 1)
        or (result == "incorrect" and score == 0)
        or (result == "partial" and 0 < score < 1)
    )


def submission_help_snapshot(context: dict) -> dict:
    """只记录提交时服务已知的帮助，不能据此推断学生没有外部帮助。"""
    keys = ("assisted", "help_snapshot", "help_level", "hint_count", "answer_revealed")
    return {key: deepcopy(context[key]) for key in keys if key in context}


def fingerprint(value) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def canonical_item_id(contract: dict) -> str:
    """版本、解析、题号和排版变化不增加独立题目数量。"""
    explicit = contract.get("canonical_item_id")
    if explicit:
        return str(explicit)[:160]

    def clean(value):
        if isinstance(value, str):
            return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))
        if isinstance(value, dict):
            return {str(key): clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    # 答案与解析可能被重评纠正，不纳入实质任务身份。
    task = {key: clean(contract.get(key)) for key in ("course_id", "stem", "options", "question_type", "images")}
    return "item:" + fingerprint(task)


def _verified_spans(answer: str, refs: list) -> list[dict]:
    verified = []
    for ref in refs or []:
        quote = ref.get("quote", "") if isinstance(ref, dict) else ""
        if not quote or quote not in answer:
            return []
        start, end = ref.get("start"), ref.get("end")
        if start is None and end is None:
            start = answer.index(quote)
            end = start + len(quote)
        elif not isinstance(start, int) or not isinstance(end, int) or answer[start:end] != quote:
            return []
        verified.append({"start": start, "end": end, "quote": quote})
    return verified


def freeze_grading_packet(
    contract: dict, answer: str, diagnosis: dict, target_ids: list[str],
    assisted: bool, help_snapshot: dict | None = None, run_id: str | None = None,
    source_version: str | None = None,
) -> dict:
    """模型提供候选，服务端校验后冻结；后续诊断不能改写这份回执。"""
    level = diagnosis["level"]
    allowed = set(target_ids) & set(contract.get("concept_ids", []))
    help_state = {**deepcopy(help_snapshot or {}), "assisted": bool(assisted)}
    overall = {key: deepcopy(diagnosis.get(key)) for key in ("result", "score", "confidence", "level", "grading_confidence")}
    criteria = {
        str(item["id"]): item for item in contract.get("assessment_criteria", [])
        if isinstance(item, dict) and item.get("id") and item.get("concept_id") in allowed
        and item.get("dimension") in LEVELS and str(item.get("description", "")).strip()
    }
    observations = []
    if len(set(contract.get("concept_ids", []))) == 1 and allowed:
        # 单概念原题可以按整题归因；综合题不能因只选择一个目标而走此捷径。
        observations.append({
            "concept_id": next(iter(allowed)), "dimension": level, "criterion_id": "whole_item",
            "result": diagnosis["result"], "score": diagnosis.get("score"),
            "confidence": diagnosis["confidence"], "attribution": "direct",
            "source_span_refs": [{"start": 0, "end": len(answer), "quote": answer}],
            "assessment_method": "rule_and_model" if diagnosis.get("grading_confidence") is not None else "model_grading",
            "rationale": "单概念题的整体作答与冻结判分。",
        })
    else:
        grouped = {}
        for proposed in diagnosis.get("concept_results", []):
            criterion = criteria.get(str(proposed.get("criterion_id", "")))
            if criterion is None:
                continue
            if proposed.get("concept_id") != criterion["concept_id"] or proposed.get("dimension") != criterion["dimension"]:
                continue
            if LEVELS.index(criterion["dimension"]) > LEVELS.index(level):
                continue
            grouped.setdefault(str(criterion["id"]), []).append(proposed)
        for criterion_id, proposals in grouped.items():
            criterion = criteria[criterion_id]
            # 重复或冲突的同一要点不通过；防止一条原答被重复加分。
            if len(proposals) != 1:
                continue
            proposed = proposals[0]
            spans = _verified_spans(answer, proposed.get("source_span_refs", []))
            result = proposed.get("result", "not_observed")
            direct = bool(spans) and result in RESULTS
            score = proposed.get("score")
            if direct and not _consistent_score(result, score):
                direct = False
            observations.append({
                "concept_id": criterion["concept_id"], "dimension": criterion["dimension"],
                "criterion_id": criterion_id, "result": result if direct else "not_observed",
                "score": score if direct else None, "confidence": float(proposed.get("confidence", 0)),
                "attribution": "direct" if direct else "unresolved", "source_span_refs": spans,
                "assessment_method": "model_grading_against_contract_criterion",
                "criterion": deepcopy(criterion), "rationale": str(proposed.get("rationale", "")),
            })
    observed_ids = {item["concept_id"] for item in observations}
    for cid in sorted(allowed - observed_ids):
        observations.append({"concept_id": cid, "dimension": level, "criterion_id": "unresolved",
            "result": "not_observed", "score": None, "confidence": 0.0, "attribution": "unresolved",
            "source_span_refs": [], "assessment_method": "no_local_evidence", "rationale": "无可核查的逐概念评分依据，保留未知。"})
    for item in observations:
        # 准入由服务计算；confidence只是启发式门槛，不解释为校准概率。
        item["eligible_for_mastery"] = bool(
            not assisted and overall["result"] != "ungraded"
            and float(overall.get("confidence") or 0) >= 0.8
            and item["attribution"] == "direct" and item["result"] in RESULTS
            and item["confidence"] >= 0.8 and _consistent_score(item["result"], item["score"])
        )
    value = {
        "schema_version": 1, "question_id": contract["id"],
        "source_version": source_version or "contract:" + str(contract.get("version", 1)) + ":grading:1",
        "canonical_item_id": canonical_item_id(contract), "contract_fingerprint": fingerprint(contract),
        "answer_fingerprint": fingerprint(answer), "overall": overall, "help_snapshot": help_state,
        "concepts": observations, "run_id": run_id,
    }
    return GradingPacket(**value, fingerprint=fingerprint(value)).model_dump()


def validate_packet(packet: dict) -> dict:
    packet = GradingPacket.model_validate(packet).model_dump()
    digest = packet.pop("fingerprint")
    if fingerprint(packet) != digest:
        raise ValueError("冻结评分回执已被修改，必须通过正式重评生成新版本")
    packet["fingerprint"] = digest
    return packet


def _state_snapshot(state, db=None):
    if state is None:
        return {"mastery": dict.fromkeys(LEVELS), "evidence_ids": [], "evidence_questions": [], "misconceptions": []}
    value = {key: deepcopy(getattr(state, key)) for key in ("mastery", "evidence_ids", "evidence_questions", "misconceptions")}
    if db is not None:
        from app.domains.courses.models import Question
        from app.domains.courses.catalog import question_contract

        # 旧状态用题号去重；首次启用局部账本时转换成实质任务身份，防止旧题再算一份。
        value["evidence_questions"] = list(dict.fromkeys(
            canonical_item_id(question_contract(question)) if (question := db.get(Question, qid)) is not None else qid
            for qid in value["evidence_questions"]
        ))
    return value


def persist_concept_links(db, attempt, session, packet: dict) -> list[EvidenceConceptLink]:
    """同事务保存原始作答与归因；同来源版本修订覆盖旧关联。"""
    packet = validate_packet(packet)
    if attempt.question_id != packet["question_id"] or fingerprint(attempt.answer_text) != packet["answer_fingerprint"]:
        raise ValueError("评分回执与原始作答不一致")
    source_ref = "attempt:" + attempt.id
    existing = list(db.scalars(select(EvidenceConceptLink).where(EvidenceConceptLink.source_ref == source_ref)))
    active_keys, rows = set(), []
    baseline_by_concept = {}
    for item in sorted(packet["concepts"], key=lambda value: (value["concept_id"], value["dimension"], value["criterion_id"])):
        cid = item["concept_id"]
        if cid not in baseline_by_concept:
            prior = db.scalar(select(EvidenceConceptLink).where(
                EvidenceConceptLink.student_id == session.student_id, EvidenceConceptLink.concept_id == cid
            ).order_by(EvidenceConceptLink.created_at, EvidenceConceptLink.id))
            baseline_by_concept[cid] = deepcopy(prior.payload.get("legacy_baseline")) if prior else _state_snapshot(db.get(StudentState, session.student_id + ":" + cid, with_for_update=True), db)
        identifier = fingerprint([session.student_id, source_ref, cid, item["dimension"], item["criterion_id"]])
        active_keys.add(identifier)
        row = db.get(EvidenceConceptLink, identifier, with_for_update=True)
        if row and row.payload.get("packet_fingerprint") == packet["fingerprint"]:
            rows.append(row)
            continue
        if row is None:
            row = EvidenceConceptLink(id=identifier, student_id=session.student_id, course_id=session.course_id,
                session_id=session.id, source_ref=source_ref, evidence_group_id=source_ref, version=0)
            db.add(row)
        row.source_version = packet["source_version"]
        row.canonical_item_id = packet["canonical_item_id"]
        row.concept_id, row.dimension, row.criterion_id = cid, item["dimension"], item["criterion_id"]
        row.source_kind, row.attribution, row.observed_result = "graded_attempt", item["attribution"], item["result"]
        row.source_span_refs = [{**span, "source_ref": source_ref} for span in item["source_span_refs"]]
        row.help_snapshot, row.eligible_for_mastery = deepcopy(packet["help_snapshot"]), item["eligible_for_mastery"]
        row.payload = {**deepcopy(item), "packet_fingerprint": packet["fingerprint"],
            "answer_fingerprint": packet["answer_fingerprint"], "contract_fingerprint": packet["contract_fingerprint"],
            "run_id": packet["run_id"], "legacy_baseline": baseline_by_concept[cid]}
        row.version += 1
        rows.append(row)
    for stale in existing:
        if stale.id not in active_keys:
            stale.eligible_for_mastery = False
            stale.attribution, stale.observed_result = "unresolved", "not_observed"
            stale.source_version = packet["source_version"]
            stale.payload = {**stale.payload, "eligible_for_mastery": False, "attribution": "unresolved",
                "result": "not_observed", "score": None, "superseded_by_packet": packet["fingerprint"]}
            stale.version += 1
            rows.append(stale)
    db.flush()
    from app.domains.memory.recording.facts import record_evidence_fact

    for row in rows:
        record_evidence_fact(db, row)
    return rows


def load_mastery_evidence(
    db, *, packet: dict, student_id: str, concept_ids: list[str], attempt_id: str,
) -> Iterator[tuple[str, list[MasteryEvidence]]]:
    """按知识点惰性读取账本；每组交给学生域锁状态并归约后，再读取下一组。"""
    validate_packet(packet)
    # 新版本可以撤销此前的概念；仍要重算原来源曾经影响过的状态。
    previous_ids = db.scalars(select(EvidenceConceptLink.concept_id).where(
        EvidenceConceptLink.student_id == student_id,
        EvidenceConceptLink.source_ref == "attempt:" + attempt_id,
    )).all()
    for cid in sorted(set(concept_ids) | set(previous_ids)):
        links = db.scalars(select(EvidenceConceptLink).where(
            EvidenceConceptLink.student_id == student_id,
            EvidenceConceptLink.concept_id == cid,
        ).order_by(EvidenceConceptLink.created_at, EvidenceConceptLink.id)).all()
        yield cid, [MasteryEvidence(
            source_ref=link.source_ref,
            evidence_group_id=link.evidence_group_id,
            canonical_item_id=link.canonical_item_id,
            dimension=link.dimension,
            created_at=link.created_at,
            eligible_for_mastery=link.eligible_for_mastery,
            score=link.payload.get("score"),
            legacy_baseline=deepcopy(link.payload.get("legacy_baseline")),
        ) for link in links]
