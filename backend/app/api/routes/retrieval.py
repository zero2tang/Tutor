"""题库、资料检索及知识图谱查询接口。"""

from fastapi import APIRouter, Request
from sqlalchemy import select

from app.api.dependencies import services
from app.infrastructure.persistence.database import db_session
from app.domains.courses.questions.policy import can_search
from app.domains.courses.retrieval.scope import matches_scope

router = APIRouter(prefix="/api")


@router.get("/search/questions")
async def search_questions(
    request: Request,
    q: str = "",
    course_id: str = "mechanics",
    concept_ids: str = "",
    mode: str = "keyword",
    coverage: str = "any",
    question_type: str | None = None,
    limit: int = 10,
):
    if course_id == "junior_algebra":
        from app.domains.courses.models import Question
        from app.domains.courses.catalog import public_question

        with db_session() as db:
            rows = db.scalars(
                select(Question).where(Question.course_id == course_id)
            ).all()
            wanted = set(filter(None, concept_ids.split(",")))
            items = [
                public_question(row)
                for row in rows
                if can_search(row) and (not q or q in row.stem or q in row.id)
                and matches_scope(row, concept_ids=wanted, coverage=coverage, question_type=question_type)
            ]
        return {
            "items": [
                {
                    "id": item["id"],
                    "title": "课程习题",
                    "text": item["stem"],
                    "question": item,
                    "score": 1.0,
                }
                for item in items[: max(1, min(limit, 30))]
            ],
            "channels_used": ["course_catalog"],
            "timings": {},
            "degraded": False,
            "reason": "",
        }
    return await services(request)["retrieval"].retrieve(
        q,
        course_id=course_id,
        concept_ids=[v for v in concept_ids.split(",") if v],
        mode=mode,
        coverage=coverage,
        question_type=question_type,
        top_k=limit,
        kind="questions",
    )


@router.get("/retrieval/rag")
async def rag(
    request: Request,
    q: str,
    concept_ids: str = "",
    course_id: str = "mechanics",
    limit: int = 6,
):
    return await services(request)["retrieval"].retrieve(
        q,
        course_id=course_id,
        concept_ids=[v for v in concept_ids.split(",") if v],
        top_k=limit,
    )


@router.get("/retrieval/graph")
async def graph_rag(
    request: Request,
    q: str,
    seed_concept_ids: str = "",
    max_hops: int = 1,
    course_id: str = "mechanics",
    limit: int = 6,
):
    return await services(request)["retrieval"].graph_retrieve(
        q,
        course_id=course_id,
        seed_concept_ids=[v for v in seed_concept_ids.split(",") if v],
        max_hops=max_hops,
        top_k=limit,
    )


@router.get("/graph")
async def graph(
    request: Request,
    concept_ids: str = "",
    max_hops: int = 1,
    course_id: str = "mechanics",
):
    catalog = services(request)["catalog"]
    catalog.course(course_id)
    if course_id == "junior_algebra":
        from app.domains.courses.models import Concept, ConceptRelation

        with db_session() as db:
            nodes = db.scalars(
                select(Concept).where(Concept.course_id == course_id)
            ).all()
            relations = db.scalars(
                select(ConceptRelation).where(ConceptRelation.course_id == course_id)
            ).all()
            return {
                "nodes": [{"id": node.id, "name": node.name} for node in nodes],
                "edges": [
                    {
                        "source": edge.source_id,
                        "target": edge.target_id,
                        "relation_type": edge.relation_type,
                        "origin": edge.origin,
                        "verified": edge.verified,
                    }
                    for edge in relations
                ],
                "seed_ids": [],
            }
    ids = [value for value in concept_ids.split(",") if value]
    if not ids:
        ids = (
            ["mechanics:c19", "mechanics:c160"]
            if course_id == "mechanics"
            else [c["id"] for c in catalog.concepts(course_id) if c["question_count"]][
                :2
            ]
        )
    for identifier in ids:
        catalog.concept(identifier, course_id)
    return await services(request)["graph"].expand(
        course_id, ids[:8], max_hops=max_hops
    )
