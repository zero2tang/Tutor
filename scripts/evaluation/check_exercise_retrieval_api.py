"""用真实MySQL和索引验证HTTP契约与构建重放；不启动或重启用户服务。"""

import argparse
import asyncio
from dataclasses import replace
import json
from pathlib import Path
from urllib.parse import urlparse
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from app.application.model_registry import register_models
from app.infrastructure.persistence.database import db_session, create_tables
from app.infrastructure.clients.llm import ModelClient
from app.infrastructure.clients.search import SearchClient
from app.domains.courses.retrieval.service import RetrievalService
from app.application.question_retrieval.service import QuestionRetrievalService
from app.application.question_retrieval.build import QuestionSearchBuilder
from app.domains.sessions.service import SessionService
from app.domains.sessions.models import LearningSession
from app.domains.courses.questions.search.models import QuestionSearchProjection
from app.api.routes.question_retrieval import router
from app.api.routes.retrieval import router as legacy_router


def private_fields(value):
    forbidden = {"answer", "explanation", "analysis", "analysis_images", "location", "_assets", "_source_hash"}
    if isinstance(value, dict):
        return bool(forbidden & value.keys()) or any(private_fields(v) for v in value.values())
    if isinstance(value, list):
        return any(private_fields(v) for v in value)
    return False


async def replay(service, builder, dataset):
    network_calls = []
    original = service.models._post
    async def recorded(url, key, body, **kwargs):
        network_calls.append({"model": body.get("model")})
        return await original(url, key, body, **kwargs)
    service.models._post = recorded
    with db_session() as db:
        before = db.scalar(select(func.count()).select_from(QuestionSearchProjection))
    result = await builder.build("math", dataset["question_ids"], visual=True)
    with db_session() as db:
        after = db.scalar(select(func.count()).select_from(QuestionSearchProjection))
    assert not network_calls and before == after
    return {"upstream_model_requests": len(network_calls), "new_projection_rows": after - before, "build": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "data/reports/exercise_retrieval/api-and-replay.json")
    parser.add_argument("--cache-only", action="store_true", help="仅允许本地 ES 和已保存模型缓存，禁止新上游请求")
    args = parser.parse_args()
    def make_models():
        instance = ModelClient()
        if args.cache_only:
            assert urlparse(instance.settings.es_url).hostname in {"localhost", "127.0.0.1", "::1"}
            async def reject_external(*a, **k):
                raise AssertionError("本次验收禁止外部模型请求；请先核对缓存")
            instance._post = reject_external
        return instance
    create_tables(register_models())
    dataset = json.loads((ROOT / "data/evaluation/exercise_retrieval/pilot-v1.json").read_text(encoding="utf-8"))
    models, search = make_models(), SearchClient()
    # 接口验收显式启用独立试点命名空间，不改进程环境或用户服务配置。
    models.settings = replace(models.settings, exercise_mm_enabled=True, exercise_rerank_enabled=False)
    service = QuestionRetrievalService(models, search, namespace="pilot-v1")
    sessions = SessionService()
    with db_session() as db:
        existing = db.scalar(select(LearningSession.id).where(LearningSession.student_id == "retrieval_acceptance",
            LearningSession.course_id == "math").order_by(LearningSession.created_at))
    session = sessions.get(existing) if existing else sessions.create("含图习题检索接口验收", [], "math", "retrieval_acceptance")
    app = FastAPI()
    from fastapi.responses import JSONResponse
    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse(status_code=400, content={"detail": str(exc)})
    app.include_router(router)
    app.include_router(legacy_router)
    app.state.services = {"sessions": sessions, "question_retrieval": service,
        "retrieval": RetrievalService(models, search, None, question_retrieval=service)}
    report = {"session_id": session["id"], "checks": {}, "cache_only": args.cache_only, "boundary": "in-process FastAPI TestClient + real MySQL/ES/model clients; existing service not restarted"}
    with TestClient(app) as client:
        path = f"/api/sessions/{session['id']}/exercise-search"
        result = client.post(path, json={"query_text": "math:1014", "intent": "exact"})
        assert result.status_code == 200 and result.json()["channels_used"] == ["exact_id"]
        assert not private_fields(result.json())
        report["checks"]["exact_id_public_projection"] = True
        old = client.get("/api/search/questions", params={"q": "math:1014", "course_id": "math"})
        assert old.status_code == 200 and old.json()["items"][0]["id"] == "math:1014"
        report["checks"]["legacy_get_route"] = True
        bad = client.post(path, json={"query_text": "测试", "course_id": "mechanics", "student_id": "admin"})
        assert bad.status_code == 422
        report["checks"]["caller_cannot_override_scope"] = True
        bad = client.post(path, json={"source_question_id": "math:1014", "asset_ids": ["foreign"]})
        assert bad.status_code == 400
        report["checks"]["foreign_asset_rejected"] = True
        result = client.post(path, json={"source_question_id": "math:1022", "intent": "isomorphic", "top_k": 3})
        assert result.status_code == 200 and not result.json()["degraded"]
        assert "multimodal" in result.json()["channels_used"] and not private_fields(result.json())
        assert "math:1022" not in [i["id"] for i in result.json()["items"]]
        report["checks"]["image_query_three_channels_no_source_repeat"] = True
        report["image_query_receipt"] = result.json()["receipt_id"]
        client.portal.call(models.close)
        client.portal.call(search.close)
    async def finish():
        replay_models, replay_search = make_models(), SearchClient()
        try:
            replay_service = QuestionRetrievalService(replay_models, replay_search, namespace="pilot-v1")
            builder = QuestionSearchBuilder(replay_service.store, replay_service.reader, replay_service.vision, replay_models, replay_search, replay_service.multimodal, "pilot-v1")
            report["replay"] = await replay(replay_service, builder, dataset)
        finally:
            await asyncio.gather(replay_models.close(), replay_search.close())
    asyncio.run(finish())
    path = args.output
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
