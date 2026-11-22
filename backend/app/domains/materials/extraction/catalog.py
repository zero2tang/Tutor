"""复核后才发布课程目录投影；原始提取档案不受投影状态影响。"""

from sqlalchemy import select
from app.domains.courses.models import Concept, Material, Question


def publish(db, book, section):
    payload = section.payload
    concept_id = f"{book.course_id}:tb:{section.id[:32]}"
    if not db.get(Concept, concept_id):
        db.add(Concept(id=concept_id, course_id=book.course_id, source_id=section.number,
            name=section.title, topic_type="section", module="教材第一章", summary=payload["teaching_design"]["summary"]))
    for kind in ("definitions", "exercises", "methods"):
        for item in payload[kind]:
            identifier = f"{section.id}:{item['source_id']}"
            if kind == "exercises":
                if db.get(Question, identifier):
                    continue
                # 小问与表格都进入完整题干；只投影已有原文，不生成答案。
                stem = item["stem"]
                for child in item.get("subquestions", []):
                    if child["text"] not in stem:
                        stem += "\n" + child["label"] + child["text"]
                for table in item.get("tables", []):
                    stem += "\n" + " | ".join(table["headers"]) + "\n" + "\n".join(" | ".join(row) for row in table["rows"])
                db.add(Question(id=identifier, course_id=book.course_id, source_id=f"{section.number}/{item['number']}"[:60],
                    stem=stem, options={}, answer=item.get("answer"), question_type="short_answer", cognitive_type="unknown",
                    concept_ids=[concept_id], explanation=item.get("solution") or "", source_ref=f"textbook:{book.id}/{item['source_id']}",
                    generated=False, validation={"textbook_book_id": book.id, "textbook_section_id": section.id,
                    "origin": "textbook_extraction", "source_answer_available": item.get("answer") is not None,
                    "source_refs": item["source_refs"], "reference_only": True, "availability": "reference_only",
                    "availability_note": "教材原文参考资料，未另行标定为可独立评分题。"}))
            elif not db.get(Material, identifier):
                db.add(Material(id=identifier, course_id=book.course_id, title=(item.get("name") or "教材原文")[:250],
                    text=item.get("text") or item.get("statement", ""), concept_ids=[concept_id], source_ref=f"textbook:{book.id}/{item['source_id']}",
                    origin="textbook_model_summary" if item.get("origin") == "model_summarized" else "textbook_extraction", visibility="public"))
    teaching_id = f"{section.id}:teaching"
    if not db.get(Material, teaching_id):
        db.add(Material(id=teaching_id, course_id=book.course_id, title=(section.title + "／教学模式")[:250],
            text=payload["teaching_design"]["summary"] + "\n" + "\n".join(s["description"] for s in payload["teaching_design"]["steps"]),
            concept_ids=[concept_id], source_ref=f"textbook:{book.id}/{section.number}", origin="textbook_model_summary", visibility="public"))


def withdraw(db, section):
    # 历史问题和模型回执保留；撤回的是通用检索可用性，不删除教材事实。
    for row in db.scalars(select(Material).where(Material.id.startswith(section.id + ":"))):
        row.visibility = "private"
    for row in db.scalars(select(Question).where(Question.id.startswith(section.id + ":"))):
        row.validation = {**row.validation, "availability": "source_incomplete", "availability_note": "原页复核发现需修订项，原始档案保留，暂不用于独立练习。"}
