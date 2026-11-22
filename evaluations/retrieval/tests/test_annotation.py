"""盲标载荷不含排名、业务题号或传给检索的解析。"""

from evaluations.retrieval.common.annotation import annotation_payload


def test_blind_aliases_and_private_reference_boundary():
    query = {
        "source_id": "math:1",
        "intent": "same_method",
        "text": "公开查询",
        "assets": [],
    }
    row = {"id": "math:2", "text": "公开候选", "assets": []}
    payload, assets = annotation_payload(
        query, [row], {"math:2": {"explanation": "私有方法参考", "answer": "5"}}
    )
    assert payload["candidates"][0]["candidate"] == "c0"
    assert "math:2" not in str(payload)
    assert "私有方法参考" in str(payload)
    assert row["text"] == "公开候选"
    assert assets == []
