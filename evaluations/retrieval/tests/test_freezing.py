"""冻结门禁拒绝过期候选池与未标注排名，不发布看似完整的结果。"""

import pytest
from evaluations.retrieval.common import freezing
from evaluations.retrieval.common.files import write_rows, write_json, read_json, digest
from evaluations.retrieval.common.config import experiments
from evaluations.retrieval.common.retrievers import representation_hash


def fixture(path, monkeypatch):
    monkeypatch.setattr(freezing, "validate_dataset", lambda *a, **k: {"valid": True})
    monkeypatch.setattr(freezing, "verify_assets", lambda *a: None)
    corpus = [
        {
            "id": "c",
            "text": "public",
            "source_hash": "h",
            "duplicate_key": "d",
            "assets": [],
        }
    ]
    query = {"id": "q", "text": "public", "assets": [], "excluded_ids": []}
    write_rows(path / "corpus.jsonl", corpus)
    write_rows(path / "queries.jsonl", [query])
    write_rows(
        path / "qrels.jsonl",
        [{"query_id": "q", "question_id": "c", "grade": 3, "review_complete": True}],
    )
    write_rows(path / "pools.jsonl", [{"query_id": "q", "candidate_ids": ["c"]}])
    write_json(
        path / "index.json",
        {
            "status": "ready",
            "fingerprint": "i",
            "config": {"corpus_hash": representation_hash(corpus)},
        },
    )
    detail = {
        "key": digest(["i", query, [e.as_dict() for e in experiments()]]),
        "candidate_ids": ["c"],
        "runs": {
            e.id: {"status": "completed", "fingerprint": "i", "rankings": [{"id": "c"}]}
            for e in experiments()
        },
    }
    write_json(path / "work/pools/q.json", detail)
    for name in ("manifest", "request", "models"):
        write_json(path / (name + ".json"), {})


def test_freeze_binds_files_and_complete_pool(tmp_path, monkeypatch):
    fixture(tmp_path, monkeypatch)
    result = freezing.freeze(tmp_path)
    assert result["unjudged_pool_pairs"] == 0
    assert set(result["files"]) >= {"queries.jsonl", "corpus.jsonl", "qrels.jsonl"}


def test_modified_query_invalidates_old_pool(tmp_path, monkeypatch):
    fixture(tmp_path, monkeypatch)
    write_rows(tmp_path / "queries.jsonl", [{"id": "q", "text": "changed"}])
    with pytest.raises(ValueError, match="当前查询"):
        freezing.freeze(tmp_path)


def test_unjudged_top_twenty_is_not_frozen(tmp_path, monkeypatch):
    fixture(tmp_path, monkeypatch)
    write_rows(
        tmp_path / "qrels.jsonl",
        [{"query_id": "q", "question_id": "c", "grade": None, "review_complete": True}],
    )
    with pytest.raises(ValueError, match="未解决"):
        freezing.freeze(tmp_path)
