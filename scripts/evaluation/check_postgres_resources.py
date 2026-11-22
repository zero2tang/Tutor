"""只读验证迁移资源、ES 固定查询与 Neo4j 关联，不调用外部模型。"""

import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))


async def main():
    from sqlalchemy import select, func, text
    from app.application.model_registry import register_models
    from app.infrastructure.config import get_settings
    from app.infrastructure.persistence.database import db_session
    from app.infrastructure.clients.search import SearchClient
    from app.infrastructure.clients.graph import GraphClient
    from app.application.question_retrieval.service import QuestionRetrievalService
    from app.domains.courses.models import Concept, Question
    from app.domains.students.service import StudentService
    from app.domains.sessions.models import LearningSession
    from app.domains.memory.models import MemoryFact
    from app.domains.courses.questions.search.models import QuestionAsset
    from app.domains.courses.questions.search.store import QuestionSearchStore

    register_models()
    target = ROOT / "data/reports/postgresql_memory_migration/resources.json"
    baseline = json.loads((target.parent / "external_before.json").read_text(encoding="utf-8"))
    report = {"passed": False, "model_requests": 0, "queries": []}

    @contextmanager
    def read_only_database():
        with db_session() as db:
            db.execute(text("SET TRANSACTION READ ONLY"))
            yield db

    class ReadOnlyQuestionStore(QuestionSearchStore):
        """保留真实目录与版本读取，回放不登记线上按需派生的题图缓存。"""
        def insert(self, model, value):
            if model is not QuestionAsset:
                raise AssertionError("只读回放出现预期外写入")
            return None

    class OfflineModels:
        settings = get_settings()
        async def embed(self, *args, **kwargs):
            report["model_requests"] += 1
            raise AssertionError("资源回放禁止产生新向量请求")
        async def chat(self, *args, **kwargs):
            report["model_requests"] += 1
            raise AssertionError("资源回放禁止产生新聊天请求")

    search, graph = SearchClient(), GraphClient()
    try:
        aliases = await search.request("GET", "/_alias/stem*")
        indices = await search.request("GET", "/_cat/indices/stem*", params={"format": "json", "h": "index,docs.count,store.size"})
        assert aliases == baseline["aliases"], "ES 别名指向发生变化"
        counts = lambda rows: {row["index"]: row["docs.count"] for row in rows}
        assert counts(indices) == counts(baseline["indices"]), "ES 文档数量发生变化"
        nodes = await graph.query("MATCH (n:StemTutorConcept) RETURN n.course_id AS course_id,count(n) AS count ORDER BY course_id")
        edges = await graph.query("MATCH (n:StemTutorConcept)-[r]->(:StemTutorConcept) RETURN n.course_id AS course_id,type(r) AS relation,count(r) AS count ORDER BY course_id,relation")
        assert nodes == baseline["graph_nodes"] and edges == baseline["graph_edges"]
        report.update(es_aliases_unchanged=True, es_counts_unchanged=True, graph_unchanged=True,
                      indices=indices, graph_nodes=nodes, graph_edges=edges)
        pilot = json.loads((ROOT / "data/evaluation/exercise_retrieval/pilot-v1.json").read_text(encoding="utf-8"))
        with db_session() as db:
            original_assets = set(db.scalars(select(QuestionAsset.id)))
        retrieval = QuestionRetrievalService(OfflineModels(), search,
            store=ReadOnlyQuestionStore(database=read_only_database))
        for qid in pilot["question_ids"]:
            result = await retrieval.retrieve(qid, course_id="math", mode="keyword", intent="exact", pipeline="A")
            assert result["items"] and result["items"][0]["id"] == qid
        report["exact_question_ids"] = len(pilot["question_ids"])
        for query in pilot["queries"]:
            result = await retrieval.retrieve(query.get("query_text", ""), course_id="math", mode="keyword",
                intent=query["intent"], source_question_id=query.get("source_question_id"), pipeline="A")
            for item in result["items"]:
                assert not {"answer", "answer_key", "explanation", "validation"}.intersection(item), "公开检索泄漏私有字段"
            report["queries"].append({"id": query["id"], "items": [item["id"] for item in result["items"]],
                                      "channels": result.get("channels_used"), "errors": result.get("errors", [])})
        with db_session() as db:
            assert set(db.scalars(select(QuestionAsset.id))) == original_assets, "回放不得新增派生资产"
            seed = db.scalar(select(Concept.id).where(Concept.course_id == "mechanics").order_by(Concept.id))
            sessions = db.scalar(select(func.count()).select_from(LearningSession))
            facts = db.scalar(select(func.count()).select_from(MemoryFact))
            generated = list(db.scalars(select(Question).where(Question.generated.is_(True))))
            report["retained_generated_questions"] = len(generated)
            assert sessions == facts == 0, "目标库出现历史或验收学生数据"
        expansion = await graph.expand("mechanics", [seed], max_hops=1)
        assert expansion["nodes"] and seed in {node["id"] for node in expansion["nodes"]}
        report["graph_expansion"] = {"nodes": len(expansion["nodes"]), "edges": len(expansion["edges"])}
        profile = StudentService().profile("junior_algebra", student_id="local")
        assert profile["attempt_count"] == profile["assessed_concepts"] == 0 and not profile.get("step_learning")
        assert all(row["status"] == "unassessed" and all(v is None for v in row["mastery"].values()) for row in profile["states"])
        assert report["model_requests"] == 0
        report.update(new_student_unassessed=True, historical_student_rows=0,
                      asset_registration="suppressed_in_read_only_replay", asset_rows_unchanged=True, passed=True)
    except BaseException as exc:
        report.update(error_type=type(exc).__name__, error=str(exc)[:800])
        raise
    finally:
        await search.close()
        await graph.close()
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"passed": report["passed"], "report": str(target)}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
