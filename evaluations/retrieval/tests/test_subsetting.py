"""派生数据集不可借用未复判标签，也不可拆开来源题族。"""

from evaluations.retrieval.common.subsetting import eligible_families
from evaluations.retrieval.common.costs import summarize_costs
from evaluations.retrieval.common.files import write_rows


def test_eligibility_needs_two_complete_positive_queries():
    queries = [{"id": "q1", "source_group": "f"}, {"id": "q2", "source_group": "f"}]
    pools = [{"query_id": q["id"], "candidate_ids": ["a", "b"]} for q in queries]
    labels = [
        {
            "query_id": q["id"],
            "question_id": c,
            "grade": 3 if c == "a" else 0,
            "review_complete": True,
        }
        for q in queries
        for c in ("a", "b")
    ]
    assert eligible_families(queries, labels, pools) == [queries]
    labels[-1]["review_complete"] = False
    assert not eligible_families(queries, labels, pools)
    labels[-1]["review_complete"] = True
    labels[-2]["grade"] = 0
    assert not eligible_families(queries, labels, pools)
    assert not eligible_families(queries[:1], labels, pools)


def test_inherited_costs_are_separate_from_incremental(tmp_path):
    receipt = {
        "role": "label",
        "status": "completed",
        "metrics": {"requests": 1, "usage": {"total_tokens": 100}},
    }
    write_rows(tmp_path / "work/usage.jsonl", [receipt])
    write_rows(tmp_path / "work/inherited/usage.jsonl", [receipt, receipt])
    groups = summarize_costs(tmp_path)["groups"]
    assert groups["label"]["total_tokens"] == 100
    assert groups["inherited_parent/label"]["total_tokens"] == 200
