"""验证每个实验真实调用的底层通道及失败传播。"""

from types import SimpleNamespace
import pytest
from evaluations.retrieval.common.files import write_rows, write_json
from evaluations.retrieval.common.config import experiments
from evaluations.retrieval.common.retrievers import Retriever


@pytest.fixture
def harness(tmp_path):
    counts = {"text": 0, "visual": 0, "pixels": 0, "keyword": 0, "knn": 0}
    settings = SimpleNamespace(
        embedding_model="text-embedding-v4",
        embedding_dim=1024,
        embedding_url="text",
        exercise_mm_model="qwen3-vl-embedding",
        exercise_mm_dim=1024,
        exercise_mm_url="visual",
        llm_model="judge",
        llm_url="judge",
    )

    async def text(texts):
        counts["text"] += 1
        return [[1.0, 0.0] for _ in texts], {}

    async def visual(text, images):
        counts["visual"] += 1
        return [1.0, 0.0], {}

    def pixels(assets):
        counts["pixels"] += 1
        return []

    clients = SimpleNamespace(
        models=SimpleNamespace(settings=settings, embed_tracked=text),
        mm=SimpleNamespace(embed=visual),
        query_vision=SimpleNamespace(pixels=pixels),
    )
    row = {
        "id": "math:1",
        "text": "公开题面",
        "source_hash": "hash",
        "duplicate_key": "d",
        "assets": [],
    }
    write_rows(tmp_path / "corpus.jsonl", [row])
    r = Retriever(tmp_path, clients=clients, search=SimpleNamespace())
    write_json(
        tmp_path / "index.json", {"status": "ready", "fingerprint": r.fingerprint}
    )

    def hits():
        return {
            "hits": {
                "hits": [
                    {
                        "_id": "math:1:0",
                        "_score": 0.8,
                        "_source": {"question_id": "math:1", "source_hash": "hash"},
                    }
                ]
            }
        }

    async def keyword(*args, **kwargs):
        counts["keyword"] += 1
        return hits()

    async def knn(*args, **kwargs):
        counts["knn"] += 1
        return hits()

    r.index = SimpleNamespace(keyword=keyword, vector=knn)
    query = {"id": "q", "text": "找同结构题", "assets": [], "excluded_ids": []}
    return r, query, counts


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identifier,expected",
    [
        ("E1", (1, 0, 0, 1)),
        ("E2", (0, 0, 1, 0)),
        ("E3", (1, 0, 1, 1)),
        ("E4", (1, 1, 1, 2)),
    ],
)
async def test_only_declared_channels_are_called(harness, identifier, expected):
    r, q, counts = harness
    experiment = next(e for e in experiments() if e.id == identifier)
    result = await r.retrieve(q, experiment)
    assert tuple(counts[k] for k in ("text", "visual", "keyword", "knn")) == expected
    assert result["channels_used"] == list(experiment.channels)
    assert result["rankings"][0]["id"] == "math:1"
    assert result["candidate_count"] == 1


@pytest.mark.asyncio
async def test_failure_is_not_silently_downgraded(harness):
    r, q, _ = harness

    async def failed(*args, **kwargs):
        raise ConnectionError("test")

    r.index.vector = failed
    with pytest.raises(ConnectionError):
        await r.retrieve(q, next(e for e in experiments() if e.id == "E3"))


@pytest.mark.asyncio
async def test_rejected_source_exclusion(harness):
    r, q, _ = harness
    q["excluded_ids"] = ["math:1"]
    with pytest.raises(ValueError, match="排除"):
        await r.retrieve(q, next(e for e in experiments() if e.id == "E2"))


@pytest.mark.asyncio
async def test_index_config_mismatch_is_rejected(harness):
    r, q, _ = harness
    write_json(r.dataset / "index.json", {"status": "ready", "fingerprint": "old"})
    with pytest.raises(ValueError, match="不兼容"):
        await r.retrieve(q, experiments()[0])
