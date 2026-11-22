"""验证数学关系来源、向量空间、公开边界和失败恢复，不以模型打分证明准确率。"""

from contextlib import contextmanager
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json
import httpx
from PIL import Image
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.infrastructure.persistence.database import Base
from app.infrastructure.persistence.model_calls import ModelCall
from app.domains.courses.models import Course, Question
from app.domains.courses.questions.search.models import QuestionAsset, QuestionRepresentation, QuestionSearchProjection
from app.domains.courses.questions.search.store import QuestionSearchStore
from app.domains.courses.questions.search.schemas import VisualExtraction, Evidence
from app.domains.courses.questions.search.representation import (
    REPRESENTATION_VERSION, public_snapshot, source_hash, math_assets, retrieval_text, clean_text, text_views, validate_extraction, digest, duplicate_key,
)
from app.infrastructure.documents.question_images import QuestionImageReader
from app.application.question_retrieval.assets import bind_assets
from app.application.question_retrieval.service import QuestionRetrievalService
from app.application.question_retrieval.rerank import select_candidates
from app.application.question_retrieval.vision import QuestionVision
from app.application.agent.tools import TeachingTools
from app.agent.tools.contracts import ToolContext
from app.domains.courses.retrieval.fusion import fuse
from app.domains.courses.retrieval.service import RetrievalService
from app.api.routes.question_retrieval import ExerciseQuery
from native_tools import invoke_tool


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    @contextmanager
    def database():
        with Session(engine, expire_on_commit=False) as db:
            with db.begin():
                yield db
    monkeypatch.setattr("app.domains.courses.catalog.db_session", database)
    monkeypatch.setattr("app.domains.courses.questions.search.representation.ROOT", tmp_path)
    root = tmp_path / "data/raw/xes3g5m/metadata/images"
    root.mkdir(parents=True)
    Image.new("RGB", (10, 10), "white").save(root / "question_1-image_0.png")
    Image.new("RGB", (10, 10), "white").save(root / "analysis_1-image_0.png")
    with database() as db:
        db.add(Course(id="math", title="数学", description="", source_ref="fixture"))
        db.add(Course(id="other", title="其他", description="", source_ref="fixture"))
        for qid, stem in [("math:1", "计算角度 ![图](/api/assets/math/question_1-image_0.png)"), ("math:2", "求三角形面积"), ("other:1", "其他课程")]:
            db.add(Question(id=qid, course_id=qid.split(":")[0], source_id=qid.split(":")[1], stem=stem, options={}, answer="秘密答案",
                explanation="秘密解析", question_type="fill", concept_ids=[], source_ref="fixture", version=1, generated=False, validation={}))
    store = QuestionSearchStore(database)
    reader = QuestionImageReader([root])
    return store, reader, root


class Models:
    def __init__(self):
        self.settings = SimpleNamespace(llm_model="vision-test", embedding_model="text-test", embedding_dim=2, embedding_url="text-v1",
            exercise_mm_model="qwen3-vl-embedding", exercise_mm_dim=2, exercise_mm_url="mm-v1", exercise_mm_enabled=False,
            exercise_rerank_enabled=False, exercise_search_courses="math", exercise_candidates=20, exercise_rerank_limit=6)
        self.embeds = self.chats = 0
    async def embed(self, texts):
        self.embeds += 1
        return [[1.0, 0.0] for _ in texts]
    async def chat(self, *args, **kwargs):
        self.chats += 1
        return {"choices": [{"message": {"content": VisualExtraction().model_dump_json()}}], "usage": {"prompt_tokens": 10}}


class Search:
    QUESTIONS = "legacy"
    def __init__(self, hits=None):
        self.hits = hits or []
        self.requests = []
    async def request(self, method, path, **kwargs):
        self.requests.append((path, kwargs))
        return {"hits": {"hits": self.hits}}


class Multimodal:
    space = {"model": "visual-test", "dimension": 2}
    calls = 0
    async def embed(self, text, images):
        self.calls += 1
        return [1.0, 0.0], {"cache_hit": False}


def hit(qid, **extra):
    return {"_id": qid, "_score": 1, "_source": {"question_id": qid, "course_id": "math", "version": 1, **extra}}


def service(setup, search=None):
    store, reader, _ = setup
    return QuestionRetrievalService(Models(), search or Search(), store=store, reader=reader, multimodal=Multimodal())


def test_qwen_shortlist_preserves_vector_head_and_bm25_only_candidates():
    vector = [{"id": f"v{i}", "ranks": {"text_vector": i}} for i in range(1, 18)]
    keyword = [{"id": f"k{i}", "ranks": {"keyword": i}} for i in range(2, 14)]
    vector[0]["ranks"]["keyword"] = 1
    ids = [item["id"] for item in select_candidates(keyword + list(reversed(vector)), 20)]
    assert ids == [f"v{i}" for i in range(1, 13)] + [f"k{i}" for i in range(2, 10)]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_qwen_reranks_full_hybrid_subset_before_top_k_and_falls_back(setup, monkeypatch, failure):
    retriever = service(setup, Search([hit("math:1"), hit("math:2")]))
    retriever.settings.exercise_rerank_enabled = True
    retriever.settings.exercise_rerank_url = "http://127.0.0.1:8019"
    async_client = httpx.AsyncClient

    def respond(request):
        assert str(request.url) == "http://127.0.0.1:8019/rerank"
        payload = json.loads(request.content)
        assert payload["intent"] == "same_method"
        assert [row["id"] for row in payload["candidates"]] == ["math:1", "math:2"]
        assert payload["candidates"][0]["images"][0].startswith("data:image/png;base64,")
        assert "秘密" not in str(payload)
        scores = [{"id": "math:1", "score": -2.0}, {"id": "math:2", "score": 2.0}]
        return httpx.Response(200, json={"model": "Qwen3-VL-Reranker-2B", "scores": scores[:1] if failure else scores,
                                         "elapsed_ms": 1.0, "images_scored": 1, "max_tokens": 40})

    monkeypatch.setattr("app.infrastructure.clients.reranker.httpx.AsyncClient",
        lambda **kwargs: async_client(transport=httpx.MockTransport(respond), **kwargs))
    result = await retriever.retrieve("相似题", course_id="math", top_k=1, intent="same_method")
    assert retriever.models.chats == 0
    assert [item["id"] for item in result["items"]] == (["math:1"] if failure else ["math:2"])
    if failure:
        assert "rerank:ValueError" in result["reason"]
        assert "rerank_score" not in result["items"][0]
    else:
        assert result["usage"]["rerank"]["candidate_count"] == 2
        assert result["items"][0]["rerank_model"] == "Qwen3-VL-Reranker-2B"
        assert result["items"][0]["match_kind"] == "candidate"


@pytest.mark.asyncio
async def test_agent_exercise_search_defaults_to_hybrid_but_can_choose_keyword(setup, monkeypatch):
    retriever = service(setup, Search([hit("math:2")]))
    retriever.settings.exercise_rerank_enabled = True
    retriever.settings.exercise_rerank_url = "http://127.0.0.1:8019"
    score = AsyncMock(return_value=({"math:2": 2.0}, {"model": "Qwen3-Reranker-0.6B"}))
    monkeypatch.setattr("app.infrastructure.clients.reranker.LocalReranker.score", score)
    tools = TeachingTools(RetrievalService(retriever.models, retriever.search, None, question_retrieval=retriever))
    # 内存 SQLite 不跨线程，工具范围校验由其他课程范围测试覆盖。
    monkeypatch.setattr(tools, "_validate", lambda *_: None)
    scope = ToolContext(course_id="math")
    hybrid = await invoke_tool(tools, "exercise", "search_exercises", {"query": "相似题"}, scope)
    keyword = await invoke_tool(tools, "exercise", "search_exercises", {"query": "三角形", "mode": "keyword"}, scope)
    assert set(hybrid["channels_used"]) == {"keyword", "text_vector"}
    assert hybrid["items"][0]["rerank_model"] == "Qwen3-Reranker-0.6B"
    assert keyword["channels_used"] == ["keyword"]
    assert "rerank" not in keyword["usage"]
    score.assert_awaited_once()


def fact(asset_id=None, text="AD垂直BC"):
    return {"text": text, "relation": "perpendicular", "objects": ["AD", "BC"], "evidence": {
        "asset_id": asset_id, "region": [0.1, 0.1, 0.3, 0.4] if asset_id else None,
        "evidence_kind": "explicit_diagram_marker" if asset_id else "explicit_text", "quote": "计算角度"}}


@pytest.mark.parametrize("bbox", [[0, 0, 0, 1], [-1, 0, 1, 1], [0, 0, 1.1, 1]])
def test_evidence_regions_are_real_normalized_rectangles(bbox):
    with pytest.raises(ValueError):
        Evidence(asset_id="a", region=bbox, evidence_kind="readable_value", quote="1")


def test_model_cannot_approve_its_own_conditions():
    with pytest.raises(ValueError):
        VisualExtraction(review_status="approved")
    with pytest.raises(ValueError):
        ExerciseQuery(query_text="test", course_id="other", student_id="admin")


def test_urls_are_removed_and_long_text_is_split_without_loss():
    original = "条件 ![题图](/api/assets/math/question_1-image_0.png) https://bad.test/x 所求"
    cleaned = clean_text(original)
    assert "http" not in cleaned and "/api" not in cleaned and "所求" in cleaned
    text = "条件" * 16000 + "最后所求"
    assert "".join(text_views(text)) == text
    assert all(len(v) <= 10000 for v in text_views(text))


def test_unreviewed_conditions_never_enter_text(setup):
    store, _, _ = setup
    snap = public_snapshot(store.question("math:1", "math"))
    payload = VisualExtraction(conditions=[fact("a")]).model_dump()
    assert "AD垂直BC" not in retrieval_text(snap, payload)
    assert "AD垂直BC" in retrieval_text(snap, payload, approved=True)
    payload["conditions"][0]["evidence"]["evidence_kind"] = "unknown"
    assert "AD垂直BC" not in retrieval_text(snap, payload, approved=True)


def test_wrong_asset_and_invented_native_quote_are_rejected(setup):
    store, _, _ = setup
    snap = public_snapshot(store.question("math:1", "math"))
    with pytest.raises(ValueError):
        validate_extraction(VisualExtraction(conditions=[fact("foreign")]).model_dump(), [], snap)
    f = fact();f["evidence"]["quote"] = "题面没有的秘密解析"
    with pytest.raises(ValueError):
        validate_extraction(VisualExtraction(conditions=[f]).model_dump(), [], snap)


def test_rrf_groups_views_before_ranking():
    result = fuse({"visual": [hit("q1"), hit("q1"), hit("q2")], "text": [hit("q2"), hit("q1")]})
    assert result[0]["rrf_score"] == pytest.approx(result[1]["rrf_score"])
    assert all(len(r["ranks"]) == 2 for r in result)


def test_only_public_stem_and_option_images_can_be_bound(setup):
    store, reader, root = setup
    q = store.question("math:1", "math")
    q.validation = {"images": ["analysis_1-image_0.png", "../private.png"]}
    assets = bind_assets(store, reader, q)
    assert len(assets) == 1 and "question_1" in assets[0]["location"]
    with pytest.raises(ValueError):
        reader.read(root.parent / "private.png")
    (root / "question_1-image_0.png").unlink()
    assert bind_assets(store, reader, q)[0]["status"] == "missing"


def test_content_hash_and_duplicate_identity_change_independently(setup):
    store, reader, root = setup
    q = store.question("math:1", "math");snap = public_snapshot(q)
    a = math_assets(snap, reader)
    Image.new("RGB", (20, 20), "white").save(root / "question_1-image_0.png")
    b = math_assets(snap, reader)
    assert source_hash(snap, a) != source_hash(snap, b)
    assert duplicate_key(snap, a) == duplicate_key(snap, b)


@pytest.mark.asyncio
async def test_exact_id_has_zero_image_embedding_and_chat_calls(setup):
    s = service(setup)
    result = await s.retrieve("1", course_id="math", source_question_id="other:1", asset_ids=["bad"])
    assert result["items"][0]["id"] == "math:1"
    assert result["channels_used"] == ["exact_id"]
    assert s.models.embeds == s.models.chats == 0 and s.search.requests == []
    assert "秘密" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.asyncio
async def test_foreign_assets_and_missing_owner_are_rejected(setup):
    s = service(setup)
    for kwargs in [{"asset_ids": ["bad"]}, {"source_question_id": "other:1"}, {"source_question_id": "math:1", "asset_ids": ["foreign"]}]:
        with pytest.raises(ValueError):
            await s.retrieve("相似题", course_id="math", **kwargs)


@pytest.mark.asyncio
async def test_all_channels_recheck_course_exclusions_and_duplicates(setup):
    s = service(setup, Search([hit("math:1"), hit("other:1"), hit("math:2")]))
    result = await s.retrieve("数学", course_id="math", excluded_question_ids=["math:1"])
    assert [i["id"] for i in result["items"]] == ["math:2"]
    assert all(v[1]["json"].get("knn", {}).get("filter", v[1]["json"].get("query"))["bool"]["filter"][0] == {"term": {"course_id": "math"}} for v in s.search.requests)


@pytest.mark.asyncio
async def test_image_only_failure_never_returns_random_topic_questions(setup):
    s = service(setup, Search([hit("math:2")]))
    with s.store.database() as db:
        db.get(Question, "math:1").stem = "![图](/api/assets/math/question_1-image_0.png)"
    (setup[2] / "question_1-image_0.png").unlink()
    result = await s.retrieve("", course_id="math", source_question_id="math:1")
    assert result["items"] == [] and result["degraded"]
    assert s.search.requests == [] and s.models.chats == 0


@pytest.mark.asyncio
async def test_visual_failure_keeps_text_and_reports_degradation(setup):
    s = service(setup, Search([hit("math:2")]))
    result = await s.retrieve("面积", course_id="math", pipeline="C")
    assert result["items"][0]["id"] == "math:2"
    assert result["degraded"] and "multimodal:ValueError" in result["reason"]


@pytest.mark.asyncio
async def test_rerank_failure_preserves_fusion_order(setup):
    s = service(setup, Search([hit("math:2")]))
    result = await s.retrieve("面积", course_id="math", pipeline="D")
    assert result["items"][0]["id"] == "math:2" and "rerank:" in result["reason"]


def projection(store, q, assets, *, config=None, fingerprint=None, namespace="main"):
    fingerprint = fingerprint or source_hash(public_snapshot(q), assets)
    config = config or {"representation": REPRESENTATION_VERSION, "text_model": "text-test", "text_dim": 2, "text_endpoint": "text-v1", "visual": {"model": "incompatible", "dimension": 2}}
    row = {"id": digest([q.id, fingerprint, namespace]), "question_id": q.id, "course_id": q.course_id, "representation_id": "native",
        "source_hash": fingerprint, "generation": "test-" + namespace, "config_hash": digest(config), "status": "published",
        "payload": {"namespace": namespace, "text": q.stem, "config": config, "extraction_status": "native_only"}}
    store.insert(QuestionSearchProjection, row)
    return row


@pytest.mark.asyncio
async def test_stale_projection_is_discarded_even_when_es_returns_it(setup):
    s = service(setup)
    q = s.store.question("math:2", "math")
    row = projection(s.store, q, [])
    s.search.hits = [hit(q.id, projection_id=row["id"], source_hash="old")]
    result = await s.retrieve("面积", course_id="math")
    assert not result["items"] and "stale_projection" in result["reason"]


@pytest.mark.asyncio
async def test_equal_dimensions_do_not_make_vector_models_compatible(setup):
    s = service(setup)
    projection(s.store, s.store.question("math:2", "math"), [])
    result = await s.retrieve("面积", course_id="math", pipeline="C")
    assert "multimodal:ValueError" in result["reason"] and s.multimodal.calls == 0


def test_review_creates_new_immutable_version_and_checks_source(setup):
    store, reader, _ = setup
    q = store.question("math:1", "math");assets = bind_assets(store, reader, q)
    fingerprint = source_hash(public_snapshot(q), assets)
    original = VisualExtraction().model_dump()
    store.insert(QuestionRepresentation, {"id": "original", "question_id": q.id, "course_id": "math", "source_hash": fingerprint,
        "asset_ids": [a["id"] for a in assets], "extractor": "test", "payload": original, "review_status": "pending", "review": {}})
    amended = VisualExtraction(conditions=[fact(assets[0]["id"])]).model_dump()
    version = store.review("original", amended, reviewer="source-audit", notes="对照原图的垂直标记", expected_hash=fingerprint)
    assert version != "original" and store.representation("original").payload == original
    assert store.representation(version).review_status == "approved"
    with pytest.raises(ValueError):
        store.review("original", amended, reviewer="test", notes="old", expected_hash="wrong")


def test_pilot_publication_cannot_replace_main_course_index(setup):
    store, _, _ = setup
    q = store.question("math:2", "math")
    projection(store, q, [], namespace="main")
    projection(store, q, [], namespace="pilot")
    store.publish("math", "test-pilot", namespace="pilot")
    assert len(store.projections("math")) == len(store.projections("math", namespace="pilot")) == 1


@pytest.mark.asyncio
async def test_withdrawal_blocks_all_channels_before_index_cleanup(setup):
    s = service(setup, Search([hit("math:2")]))
    s.store.withdraw("math:2", "math")
    assert not (await s.retrieve("面积", course_id="math"))["items"]


@pytest.mark.asyncio
async def test_validated_model_call_can_resume_without_new_request(setup):
    s = service(setup)
    q = s.store.question("math:1", "math");assets = bind_assets(s.store, s.reader, q)
    first, metrics = await s.vision.extract(public_snapshot(q), assets)
    second, cached = await s.vision.extract(public_snapshot(q), assets)
    assert first == second and cached["cache_hit"] and s.models.chats == 1


def test_build_lease_prevents_concurrent_publication_and_can_resume(setup):
    store, _, _ = setup
    identifier, token = store.claim_build("math", "main")
    with pytest.raises(ValueError, match="租约"):
        store.claim_build("math", "main")
    with pytest.raises(ValueError, match="租约"):
        store.renew_build(identifier, "wrong")
    store.renew_build(identifier, token, finish=True, receipt={"status": "failed"})
    assert store.claim_build("math", "main")[1] != token


@pytest.mark.asyncio
async def test_multimodal_api_sends_pixels_and_cache_uses_image_hash(setup, tmp_path):
    from app.infrastructure.clients.multimodal_embedding import MultimodalEmbeddingClient
    models = Models()
    models.settings.exercise_mm_dim = 256
    models.settings.embedding_key = "test-only"
    requests = []
    async def post(url, key, body, **kwargs):
        requests.append(body)
        return {"output": {"embeddings": [{"embedding": [1.0] * 256}]}, "usage": {"input_tokens": 20}}
    models._post = post
    client = MultimodalEmbeddingClient(models, models.settings)
    client.cache = tmp_path / "vectors"
    image = setup[1].read(setup[2] / "question_1-image_0.png")
    first, metrics = await client.embed("条件", [image])
    second, cached = await client.embed("条件", [image])
    assert first == second and cached["cache_hit"] and len(requests) == 1
    assert requests[0]["parameters"]["enable_fusion"] is True
    assert requests[0]["input"]["contents"][1]["image"].startswith("data:image/png;base64,")
    await client.embed("条件", [{**image, "hash": "changed"}])
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_rerank_batches_complete_candidates_instead_of_dropping_images(setup):
    s = service(setup)
    assets = bind_assets(s.store, s.reader, s.store.question("math:1", "math"))
    calls = []
    async def chat(messages, **kwargs):
        payload = json.loads(messages[1]["content"][0]["text"])
        calls.append(payload)
        matches = [{"question_id": i["question_id"], "match_kind": "topic_only", "relevance": 1, "evidence": [], "differences": [], "unknowns": []} for i in payload["candidates"]]
        return {"choices": [{"message": {"content": json.dumps({"matches": matches})}}]}
    s.models.chat = chat
    candidates = [{"id": f"math:{i}", "text": "公开题面", "_assets": assets} for i in range(10, 17)]
    matches, metrics = await s.vision.rerank("查询", assets, candidates, "isomorphic")
    assert set(matches) == {c["id"] for c in candidates}
    assert len(calls) >= 2 and sum(len(c["candidates"]) for c in calls) == 7


@pytest.mark.asyncio
async def test_source_validation_failure_has_one_bounded_repair(setup):
    s = service(setup)
    q = s.store.question("math:1", "math");assets = bind_assets(s.store, s.reader, q)
    count = 0
    async def chat(*args, **kwargs):
        nonlocal count
        count += 1
        value = VisualExtraction(conditions=[fact("wrong")]) if count == 1 else VisualExtraction()
        return {"choices": [{"message": {"content": value.model_dump_json()}}]}
    s.models.chat = chat
    await s.vision.extract(public_snapshot(q), assets)
    await s.vision.extract(public_snapshot(q), assets)
    assert count == 2
    with s.store.database() as db:
        rows = db.scalars(select(ModelCall)).all()
        assert sorted(r.status for r in rows) == ["completed", "failed"]


def test_controlled_textbook_asset_reaches_tutoring_model_without_private_images(monkeypatch):
    from app.application.agent.images import QuestionImageContext
    seen = []
    def read(asset_id, question_id, course_id):
        seen.append((asset_id, question_id, course_id))
        return {"bytes": 10, "url": "data:image/png;base64,actual-pixels"}
    monkeypatch.setattr("app.application.agent.images.public_asset_pixels", read)
    q = {"id": "book:1", "course_id": "book", "stem": "教材题", "options": {},
        "images": [{"url": "/api/assets/exercises/" + "a" * 64}], "explanation": "/api/assets/exercises/" + "b" * 64}
    parts = QuestionImageContext("book").parts(q)
    assert seen == [("a" * 64, "book:1", "book")]
    assert any(p["type"] == "image_url" for p in parts)


def test_option_image_binding_retains_option_letter(setup):
    store, reader, _ = setup
    q = store.question("math:2", "math")
    q.options = {"A": "![选项](/api/assets/math/question_1-image_0.png)"}
    q.validation = {"images": ["question_1-image_0.png"]}
    assets = math_assets(public_snapshot(q), reader)
    assert len(assets) == 1 and assets[0]["role"] == "option"
    assert assets[0]["source"]["fields"] == ["option:A"]


@pytest.mark.asyncio
async def test_seen_content_is_excluded_after_renumbering_even_if_original_withdrawn(setup):
    s = service(setup, Search([hit("math:2")]))
    with s.store.database() as db:
        first, second = db.get(Question, "math:1"), db.get(Question, "math:2")
        second.stem, second.options = first.stem, first.options
        first.validation = {"visibility": "private"}
    result = await s.retrieve("相似练习", course_id="math", excluded_question_ids=["math:1"])
    assert result["items"] == []
