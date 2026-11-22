"""按课程重建公开 ES 与 Neo4j 投影，不触碰学生交互和其他课程。"""

import asyncio
import json
import time

from sqlalchemy import select

from app.infrastructure.clients.graph import GraphClient
from app.infrastructure.clients.llm import ModelClient
from app.infrastructure.clients.search import SearchClient
from app.infrastructure.config import ROOT
from app.infrastructure.persistence.database import db_session
from app.domains.courses.models import (
    Concept,
    ConceptRelation,
    Course,
    IndexTask,
    Material,
    Question,
)
from app.domains.courses.questions.policy import can_search


def mark(course_id: str, target: str, status: str, details: dict) -> None:
    with db_session() as db:
        identifier = f"{course_id}:v1:{target}"
        task = db.get(IndexTask, identifier)
        if task is None:
            task = IndexTask(id=identifier, target=target)
            db.add(task)
        task.status, task.details = status, details


def snapshot(course_id: str) -> dict:
    """只投影公开字段，答案、解析和学生记录不会进入向量请求。"""
    with db_session() as db:
        if not db.get(Course, course_id):
            raise ValueError("请先导入课程元数据")
        concepts = [
            {"id": c.id, "name": c.name}
            for c in db.scalars(select(Concept).where(Concept.course_id == course_id))
        ]
        questions = [
            {
                "id": q.id,
                "source_id": q.source_id,
                "course_id": q.course_id,
                "title": q.stem[:100],
                "text": q.stem
                + " "
                + " ".join(f"{k}. {v}" for k, v in q.options.items()),
                "concept_ids": q.concept_ids,
                "question_type": q.question_type,
                "cognitive_type": q.cognitive_type,
                "visibility": "public",
                "source_ref": q.source_ref,
                "origin": "dataset",
                "version": q.version,
            }
            for q in db.scalars(
                select(Question).where(
                    Question.course_id == course_id, Question.generated == False
                )
            )
            if can_search(q)
        ]
        materials = [
            {
                "id": m.id,
                "course_id": m.course_id,
                "title": m.title,
                "text": m.text,
                "concept_ids": m.concept_ids,
                "source_ref": m.source_ref,
                "origin": m.origin,
                "visibility": m.visibility,
                "question_id": m.question_id,
                "version": m.version,
            }
            for m in db.scalars(
                select(Material).where(
                    Material.course_id == course_id, Material.visibility == "public"
                )
            )
        ]
        relations = [
            {
                "source_id": r.source_id,
                "target_id": r.target_id,
                "relation_type": r.relation_type,
                "origin": r.origin,
                "verified": r.verified,
            }
            for r in db.scalars(
                select(ConceptRelation).where(ConceptRelation.course_id == course_id)
            )
        ]
    return {
        "concepts": concepts,
        "questions": questions,
        "materials": materials,
        "relations": relations,
    }


async def index_documents(
    course_id: str, index: str, documents: list[dict], model, search
) -> None:
    batches = [
        (offset, documents[offset : offset + 10])
        for offset in range(0, len(documents), 10)
    ]
    queue = asyncio.Queue()
    for item in batches:
        queue.put_nowait(item)
    completed = 0

    async def worker() -> None:
        nonlocal completed
        while not queue.empty():
            _, batch = queue.get_nowait()
            vectors = await model.embed(
                [item["title"] + "\n" + item["text"] for item in batch]
            )
            # 每批单独复制文档，防止可重建快照残留大向量。
            payload = [
                {**item, "embedding": vector}
                for item, vector in zip(batch, vectors, strict=True)
            ]
            await search.bulk_index(index, payload, refresh=False)
            completed += len(batch)
            if completed % 250 < 10 or completed == len(documents):
                mark(
                    course_id,
                    "es",
                    "building",
                    {
                        "index": index,
                        "completed": completed,
                        "expected": len(documents),
                    },
                )
                print(f"{course_id} {index}: {completed}/{len(documents)}", flush=True)
            queue.task_done()

    # TaskGroup 在某一批失败时取消同组任务，避免后台索引在报告失败后继续运行。
    async with asyncio.TaskGroup() as group:
        for _ in range(min(3, len(batches))):
            group.create_task(worker())
    await search.request("POST", f"/{index}/_refresh")


async def project_course(course_id: str) -> dict:
    data = snapshot(course_id)
    model, search, graph = ModelClient(), SearchClient(), GraphClient()
    result = {"course_id": course_id}
    started = time.perf_counter()
    mark(course_id, "es", "building", {})
    mark(course_id, "neo4j", "pending", {})
    try:
        await search.ensure_indices()
        for index, kind in [
            (search.QUESTIONS, "questions"),
            (search.MATERIALS, "materials"),
        ]:
            await index_documents(course_id, index, data[kind], model, search)
        result["es"] = {
            "questions": len(data["questions"]),
            "materials": len(data["materials"]),
        }
        mark(course_id, "es", "ready", result["es"])
        mark(course_id, "neo4j", "building", {})
        await graph.synchronize(
            course_id, data["concepts"], data["questions"], data["relations"]
        )
        result["neo4j"] = {
            "concepts": len(data["concepts"]),
            "questions": len(data["questions"]),
            "relations": len(data["relations"]),
        }
        mark(course_id, "neo4j", "ready", result["neo4j"])
        result["duration_seconds"] = round(time.perf_counter() - started, 2)
        (ROOT / "data/reports" / f"{course_id}_indexing.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return result
    except Exception as exc:
        mark(
            course_id,
            "neo4j" if "es" in result else "es",
            "failed",
            {"error": type(exc).__name__, "message": str(exc)[:250]},
        )
        raise
    finally:
        await asyncio.gather(model.close(), search.close(), graph.close())
