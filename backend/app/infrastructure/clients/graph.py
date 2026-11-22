"""图查询仅使用预定义模板，所有写入限定在 StemTutor 命名空间。"""

import asyncio
from neo4j import GraphDatabase

from app.infrastructure.config import get_settings

RELATIONS = {
    "RELATED_TO": "STEM_RELATED_TO",
    "PREREQUISITE_OF": "STEM_PREREQUISITE_OF",
    "HAS_CHILD": "STEM_HAS_CHILD",
}


class GraphClient:
    def __init__(self) -> None:
        settings = get_settings()
        self.driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
            connection_timeout=8,
        )

    def _query(self, query: str, **parameters) -> list[dict]:
        records, _, _ = self.driver.execute_query(
            query, parameters_=parameters, database_="neo4j"
        )
        return [record.data() for record in records]

    async def query(self, query: str, **parameters) -> list[dict]:
        return await asyncio.to_thread(self._query, query, **parameters)

    async def health(self) -> bool:
        rows = await self.query("RETURN 1 AS ok")
        return rows[0]["ok"] == 1

    async def synchronize(
        self,
        course_id: str,
        concepts: list[dict],
        questions: list[dict],
        relations: list[dict],
    ) -> None:
        await self.query(
            "CREATE CONSTRAINT stem_tutor_concept_id IF NOT EXISTS FOR (n:StemTutorConcept) REQUIRE n.id IS UNIQUE"
        )
        await self.query(
            "CREATE CONSTRAINT stem_tutor_question_id IF NOT EXISTS FOR (n:StemTutorQuestion) REQUIRE n.id IS UNIQUE"
        )
        await self.query(
            "UNWIND $rows AS row MERGE (n:StemTutorConcept {id: row.id}) SET n.name=row.name, n.course_id=$course",
            rows=concepts,
            course=course_id,
        )
        # 图投影只接收题号与知识点，分批提交，不传整份题干和向量。
        for offset in range(0, len(questions), 500):
            await self.query(
                "UNWIND $rows AS row MERGE (q:StemTutorQuestion {id: row.id}) SET q.course_id=$course WITH q,row UNWIND row.concept_ids AS cid MATCH (c:StemTutorConcept {id:cid}) MERGE (q)-[:STEM_ASSESSES]->(c)",
                rows=[
                    {"id": q["id"], "concept_ids": q["concept_ids"]}
                    for q in questions[offset : offset + 500]
                ],
                course=course_id,
            )
        for kind, stored_kind in RELATIONS.items():
            selected = [r for r in relations if r["relation_type"] == kind]
            if selected:
                await self.query(
                    f"UNWIND $rows AS row MATCH (a:StemTutorConcept {{id:row.source_id}}), (b:StemTutorConcept {{id:row.target_id}}) MERGE (a)-[r:{stored_kind}]->(b) SET r.origin=row.origin, r.verified=row.verified, r.course_id=$course",
                    rows=selected,
                    course=course_id,
                )

    async def expand(
        self,
        course_id: str,
        seeds: list[str],
        *,
        max_hops: int = 1,
        relation_types: list[str] | None = None,
    ) -> dict:
        if not 1 <= max_hops <= 2:
            raise ValueError("图扩展仅支持 1–2 跳")
        kinds = [
            RELATIONS[k] for k in relation_types or list(RELATIONS) if k in RELATIONS
        ]
        found = set(seeds)
        frontier = list(seeds)
        edges: dict[str, dict] = {}
        for _ in range(min(max(max_hops, 1), 2)):
            rows = await self.query(
                "MATCH (a:StemTutorConcept)-[r]-(b:StemTutorConcept) WHERE a.id IN $ids AND a.course_id=$course AND b.course_id=$course AND type(r) IN $kinds RETURN startNode(r).id AS source, endNode(r).id AS target, type(r) AS kind, r.origin AS origin, r.verified AS verified, b.id AS id, b.name AS name LIMIT 60",
                ids=frontier,
                course=course_id,
                kinds=kinds,
            )
            next_frontier = []
            for row in rows:
                edge_id = f"{row['source']}:{row['kind']}:{row['target']}"
                edges[edge_id] = {
                    "source": row["source"],
                    "target": row["target"],
                    "relation_type": row["kind"].removeprefix("STEM_"),
                    "origin": row["origin"],
                    "verified": row["verified"],
                }
                if row["id"] not in found and len(found) < 70:
                    found.add(row["id"])
                    next_frontier.append(row["id"])
            frontier = next_frontier
            if not frontier:
                break
        nodes = await self.query(
            "MATCH (c:StemTutorConcept) WHERE c.id IN $ids AND c.course_id=$course RETURN c.id AS id,c.name AS name",
            ids=list(found),
            course=course_id,
        )
        return {
            "nodes": nodes,
            "edges": list(edges.values()),
            "seed_ids": seeds,
            "max_hops": min(max_hops, 2),
        }

    async def close(self) -> None:
        await asyncio.to_thread(self.driver.close)
