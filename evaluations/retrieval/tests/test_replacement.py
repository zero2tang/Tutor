"""无正例修复保留同族变体、集合归属与未知证据。"""

from evaluations.retrieval.common.files import (
    write_rows,
    write_json,
    read_rows,
    read_json,
)
from evaluations.retrieval.common.replacement import repair_missing_positives


def fixture(path, *, full=False, unknown=False):
    corpus = [
        {"id": f"c{i}", "source_group": f"f{i}", "has_image": False} for i in range(4)
    ]
    seed = {
        "source_id": "c0",
        "source_group": "f0",
        "intent": "isomorphic",
        "input_kind": "text",
        "split": "test",
    }
    queries = [{**seed, "id": f"q{i}", "excluded_ids": ["c0"]} for i in range(2)]
    pools = [
        {
            "query_id": q["id"],
            "candidate_ids": ["c1", "c2", "c3"] if full else ["c1"],
            "random_ids": [],
        }
        for q in queries
    ]
    labels = [
        {
            "query_id": p["query_id"],
            "question_id": cid,
            "grade": None if unknown and p["query_id"] == "q1" else 0,
            "review_complete": True,
        }
        for p in pools
        for cid in p["candidate_ids"]
    ]
    for name, rows in (
        ("corpus", corpus),
        ("queries", queries),
        ("pools", pools),
        ("qrels", labels),
    ):
        write_rows(path / (name + ".jsonl"), rows)
    for p in pools:
        write_json(path / "work/pools" / (p["query_id"] + ".json"), p)
    write_json(path / "work/query_seeds.json", [seed])
    write_json(path / "manifest.json", {})


def test_expand_before_replacing_any_family(tmp_path):
    fixture(tmp_path)
    result = repair_missing_positives(tmp_path)
    assert len(result["expanded_queries"]) == 2
    assert not result["replaced_families"]
    assert len(read_rows(tmp_path / "queries.jsonl")) == 2
    assert len(read_rows(tmp_path / "pools.jsonl")[0]["candidate_ids"]) == 3


def test_replace_whole_family_and_archive_evidence(tmp_path):
    fixture(tmp_path, full=True)
    result = repair_missing_positives(tmp_path)
    assert result["replaced_families"] == ["f0"]
    assert read_rows(tmp_path / "queries.jsonl") == []
    assert len(read_json(tmp_path / "review/replaced/f0.json")["queries"]) == 2
    assert read_json(tmp_path / "work/query_seeds.json")[0]["split"] == "test"


def test_uncertain_sibling_prevents_family_replacement(tmp_path):
    fixture(tmp_path, full=True, unknown=True)
    result = repair_missing_positives(tmp_path)
    assert not result["replaced_families"]
    assert result["blocked"]
    assert len(read_rows(tmp_path / "queries.jsonl")) == 2
