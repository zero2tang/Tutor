"""普通 RAG、Graph RAG 和查题共用召回逻辑与范围约束。"""

import time

from sqlalchemy import select

from app.infrastructure.clients.graph import GraphClient
from app.infrastructure.clients.llm import ModelClient
from app.infrastructure.clients.search import SearchClient
from app.infrastructure.persistence.database import db_session
from app.domains.courses.models import Question
from app.domains.courses.catalog import CatalogService, public_question
from app.domains.courses.questions.policy import can_search
from app.domains.courses.retrieval.scope import filters as scope_filters, search_scope, matches_scope
from app.domains.courses.retrieval.channels import run_channels
from app.domains.courses.retrieval.fusion import fuse
from app.infrastructure.clients.retrieval_index import RetrievalIndex


class RetrievalService:
    def __init__(self, models: ModelClient, search: SearchClient, graph: GraphClient, *, question_retrieval=None):
        """共享模型、ES 和图客户端，为所有 Agent 工具提供统一检索入口。"""
        self.models, self.search, self.graph = models, search, graph
        self.catalog = CatalogService()
        self.question_retrieval = question_retrieval
        self.index = RetrievalIndex(search)

    @staticmethod
    def filters(
        course_id: str,
        concept_ids: list[str],
        coverage: str = "any",
        question_type: str | None = None,
    ) -> list:
        """构建课程与公开可见性过滤；any 匹配任一知识点，all 要求同时覆盖全部目标。"""
        return scope_filters(course_id, concept_ids, coverage, question_type)

    async def retrieve(
        self,
        query: str,
        *,
        course_id: str = "mechanics",
        concept_ids: list[str] | None = None,
        top_k: int = 6,
        kind: str = "materials",
        mode: str | None = "hybrid",
        coverage: str = "any",
        question_type: str | None = None,
        excluded_question_ids: list[str] | None = None,
        intent: str = "isomorphic",
        asset_ids: list[str] | None = None,
        source_question_id: str | None = None,
        session_id: str | None = None,
        student_id: str = "local",
    ) -> dict:
        """在公开教学材料或习题索引中检索，返回内容、来源、通道与耗时。

        关键词通道先精确查题号；非空混合查询再并行执行 BM25 与向量召回，
        用倒数排名融合，并让精确 ID 优先。通道失败会显式降级，题目返回前
        从目录读取公开结构；待答题的答案和解析不作为检索结果交给模型。
        """
        if kind == "questions" and self.question_retrieval and self.question_retrieval.enabled(course_id):
            return await self.question_retrieval.retrieve(query, course_id=course_id, concept_ids=concept_ids,
                top_k=top_k, mode="hybrid" if mode is None else mode, coverage=coverage, question_type=question_type,
                excluded_question_ids=excluded_question_ids, intent=intent, asset_ids=asset_ids,
                source_question_id=source_question_id, session_id=session_id, student_id=student_id)
        if asset_ids or source_question_id:
            raise ValueError("当前课程未启用含图习题检索")
        self.catalog.course(course_id)
        if mode is None:
            mode = "keyword"
        if mode not in {"keyword", "hybrid"} or coverage not in {"any", "all"}:
            raise ValueError("无效的检索方式或知识点覆盖条件")
        top_k = min(max(int(top_k), 1), 20)
        concepts = concept_ids or []
        for identifier in concepts:
            self.catalog.concept(identifier, course_id)
        index = self.search.QUESTIONS if kind == "questions" else self.search.MATERIALS
        exclusions = excluded_question_ids or []
        scope = search_scope(course_id, concepts, coverage, question_type, exclusions, questions=kind == "questions")
        start = time.perf_counter()
        timings = {}
        errors = []
        channels = []
        candidates = max(top_k * 3, 20)
        exact_ids = set()

        async def keyword():
            if query.strip():
                exact = await self.index.exact(index, scope, query.strip(), course_id, candidates)
                if exact["hits"]["hits"]:
                    exact_ids.update(hit["_id"] for hit in exact["hits"]["hits"])
                    return exact
            return await self.index.keyword(index, scope, query, candidates,
                fields=("title^3", "text"), course_id=course_id)

        async def vector():
            started = time.perf_counter()
            embedding = (await self.models.embed([query]))[0]
            timings["embedding_ms"] = round((time.perf_counter() - started) * 1000, 2)
            started = time.perf_counter()
            result = await self.index.vector(index, scope, embedding, candidates)
            timings["vector_ms"] = round((time.perf_counter() - started) * 1000, 2)
            return result

        work = {"keyword": keyword}
        if query.strip() and mode == "hybrid":
            work["vector"] = vector
        channels = await run_channels(work, timings, errors)
        ranked = fuse(channels, by_question=False, exact_ids=exact_ids, stable_order=True)
        items = []
        # 精确题号是确定的查找意图，不能被题图URL或向量推荐的排名淹没。
        for record in ranked:
            identifier, row = record["id"], record["source"]
            item = {
                "id": identifier,
                "title": row.get("title", ""),
                "text": row.get("text", ""),
                "course_id": row["course_id"],
                "concept_ids": row.get("concept_ids", []),
                "source_ref": row.get("source_ref", ""),
                "origin": row.get("origin", "dataset"),
                "score": round(record["rrf_score"], 6),
                "channels": list(record["ranks"]),
                "match_kind": "exact_id" if identifier in exact_ids else "retrieval",
            }
            if kind == "questions":
                try:
                    item["question"] = self.catalog.question(identifier, course_id)
                except ValueError:
                    continue
                if not can_search(item["question"]):
                    continue
            items.append(item)
            if len(items) >= top_k:
                break
        # 降级必须如实标注，不能用数据库结果冒充 ES 或向量召回。
        if not channels and kind == "questions":
            with db_session() as db:
                rows = db.scalars(
                    select(Question).where(Question.course_id == course_id)
                ).all()
                rows = [
                    q
                    for q in rows
                    if can_search(q)
                    and matches_scope(q, concept_ids=concepts, coverage=coverage,
                        question_type=question_type, exclusions=exclusions)
                    and (not query or query in q.stem or query == q.source_id)
                ]
                items = [
                    {
                        "id": q.id,
                        "title": q.stem[:80],
                        "text": q.stem,
                        "source_ref": q.source_ref,
                        "concept_ids": q.concept_ids,
                        "question": public_question(q),
                        "channels": ["database_exact"],
                    }
                    for q in rows[:top_k]
                ]
                channels = {"database_exact": []}
        timings["total_ms"] = round((time.perf_counter() - start) * 1000, 2)
        citations = [
            {
                "id": item["id"],
                "title": item["title"],
                "source_ref": item["source_ref"],
                "origin": item.get("origin", "dataset"),
            }
            for item in items
        ]
        return {
            "items": items,
            "citations": citations,
            "channels_used": list(channels),
            "degraded": bool(errors),
            "reason": "; ".join(errors),
            "timings": timings,
        }

    async def graph_retrieve(
        self,
        query: str,
        *,
        course_id: str = "mechanics",
        seed_concept_ids: list[str] | None = None,
        top_k: int = 6,
        max_hops: int = 1,
        excluded_question_ids: list[str] | None = None,
    ) -> dict:
        """先确定种子知识点，再按图扩展结果限定普通 RAG 的材料范围。

        没有显式种子时尝试名称匹配，再从初次检索中提取种子；图服务失败
        则退回种子范围内的普通 RAG 并说明降级，不伪装为已成功使用图谱。
        """
        start = time.perf_counter()
        seeds = seed_concept_ids or [
            c["id"] for c in self.catalog.resolve_concepts(query, course_id)
        ]
        if not seeds:
            initial = await self.retrieve(query, course_id=course_id, top_k=3)
            seeds = list(
                dict.fromkeys(
                    cid for item in initial["items"] for cid in item["concept_ids"]
                )
            )[:3]
        for identifier in seeds:
            self.catalog.concept(identifier, course_id)
        try:
            graph = await self.graph.expand(course_id, seeds, max_hops=max_hops)
        except Exception as exc:
            fallback = await self.retrieve(
                query,
                course_id=course_id,
                concept_ids=seeds,
                top_k=top_k,
                excluded_question_ids=excluded_question_ids,
            )
            return {
                **fallback,
                "degraded": True,
                "reason": "图服务不可用：" + type(exc).__name__,
                "graph": {"nodes": [], "edges": [], "seed_ids": seeds},
            }
        expanded = [node["id"] for node in graph["nodes"]]
        result = await self.retrieve(
            query,
            course_id=course_id,
            concept_ids=expanded,
            top_k=top_k,
            excluded_question_ids=excluded_question_ids,
        )
        result["graph"] = graph
        result["channels_used"] = [*result["channels_used"], "neo4j"]
        result["timings"]["graph_total_ms"] = round(
            (time.perf_counter() - start) * 1000, 2
        )
        return result
