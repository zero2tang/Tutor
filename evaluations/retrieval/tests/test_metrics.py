"""使用手算排名验证指标，防止未知标签和短列表被错误解释。"""

import math
import pytest
from evaluations.retrieval.common.metrics import query_metrics, paired_bootstrap


def test_short_list_and_duplicates_use_different_precision_denominators():
    result = query_metrics(["a", "a", "x"], {"a": 3, "b": 2, "x": 0})
    assert result["recall_at_5"] == 0.5
    assert result["precision_at_5"] == 0.2
    assert result["id_precision_at_5"] == 0.5
    assert result["mrr_at_10"] == 1
    assert result["ndcg_at_10"] == pytest.approx(7 / (7 + 3 / math.log2(3)))


def test_unjudged_does_not_become_negative():
    result = query_metrics(["unknown", "a"], {"a": 3})
    assert result["judged_at_20"] == 0.5
    assert result["recall_at_5"] is None
    assert result["ndcg_at_10"] is None


def test_empty_results_with_references_are_zero_not_missing():
    result = query_metrics([], {"a": 3})
    assert result["recall_at_5"] == 0
    assert result["precision_at_5"] == 0
    assert result["ndcg_at_10"] == 0
    assert result["mrr_at_10"] == 0


def test_no_reference_is_not_perfect_recall():
    assert query_metrics(["x"], {"x": 0})["recall_at_5"] is None


def test_grade_one_has_ndcg_gain_but_is_not_recalled_positive():
    result = query_metrics(["x", "b"], {"a": 3, "b": 2, "x": 1}, hard_negatives={"x"})
    assert result["recall_at_5"] == 0.5
    assert result["mrr_at_10"] == 0.5
    assert result["hard_negative_at_10"] == 0.1


def test_cluster_bootstrap_preserves_pairs():
    rows = []
    for family in range(3):
        for variant in range(2):
            for experiment, value in (("E3", 0.75), ("E1", 0.25)):
                rows.append(
                    {
                        "source_group": str(family),
                        "query_id": f"{family}-{variant}",
                        "experiment": experiment,
                        "metrics": {"recall_at_10": value},
                    }
                )
    value = paired_bootstrap(rows, "E3", "E1", "recall_at_10", repeats=200)
    assert value["difference"] == 0.5
    assert value["ci95"] == [0.5, 0.5]
    assert value["source_groups"] == 3
