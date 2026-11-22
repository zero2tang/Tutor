"""排版引用恢复不能把图像猜测变成题面事实。"""

import pytest
from evaluations.retrieval.common.grounding import restore_literal_quotes


def value(quote):
    return {
        "ocr": [],
        "formulas": [],
        "conditions": [
            {
                "text": "条件",
                "evidence": {
                    "asset_id": None,
                    "evidence_kind": "explicit_text",
                    "quote": quote,
                },
            }
        ],
        "goals": [],
        "image_roles": {},
    }


def test_restore_only_literal_formatting_difference():
    v = value("4张纸片的周长")
    repairs = restore_literal_quotes(
        v, [], {"stem": "$$4$$张纸片的周长是多少", "options": {}}
    )
    assert v["conditions"][0]["evidence"]["quote"] == "$$4$$张纸片的周长"
    assert len(repairs) == 1


@pytest.mark.parametrize(
    "quote", ["5张纸片的周长", "四张纸片的周长", "图中有四个长方形"]
)
def test_fabricated_or_paraphrased_evidence_still_fails(quote):
    with pytest.raises(ValueError):
        restore_literal_quotes(
            value(quote), [], {"stem": "$$4$$张纸片的周长是多少", "options": {}}
        )


def test_ambiguous_formatting_match_is_not_repaired():
    with pytest.raises(ValueError):
        restore_literal_quotes(
            value("4张纸片"), [], {"stem": "$$4$$张纸片和$$4$$张纸片", "options": {}}
        )
