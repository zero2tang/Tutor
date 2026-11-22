"""教材来源投影仅校验结构与完整性，不再求解或推导教学分析。"""

from copy import deepcopy
from pathlib import Path

import pytest

from app.domains.materials.parsing.parser import parse_document
from app.domains.materials.catalog_projection import VERSION, catalog_hash, project_catalog, verify_catalog_pack


def parsed(content="1. 解方程 $2(x+1)=8$。\n\n答案：$x=99$"):
    return parse_document(
        content,
        format="markdown",
        title="自编来源投影测试",
        source_kind="self_authored",
        source_uri="fixture://source-projection",
    )


def test_projection_preserves_even_wrong_original_answer_without_computing_a_replacement():
    source = parsed()
    pack = project_catalog(source)[0]
    assert pack["version"] == VERSION
    assert pack["source_answer"]["value"] == "x=99"
    assert pack["source_solution_steps"] == ["x=99"]
    assert (
        pack["source_solution"][0]["provenance"]["origin"] == "textbook_transcription"
    )
    assert pack["status"] == "ready"
    assert pack["verification"]["result"] == "not_checked"
    assert pack["verification"]["method"] == "source_structure_integrity"
    assert pack["provenance"]["mathematics_checked"] is False
    assert (
        not pack["solution_paths"] and not pack["concepts"] and not pack["techniques"]
    )
    assert not pack["hint_ladder"] and not pack["diagnostic_probes"]
    assert "derived_answer" not in pack
    verify_catalog_pack(pack, source)


@pytest.mark.parametrize(
    "stem",
    [
        "证明三角形ABC与DEF全等，并说明所用条件。",
        "沿跑道一周后，路程和位移的区别是什么？",
        "求函数极限，并说明每一步依据。",
        "解方程 $x*x=1$。",
        "讨论参数a取不同值时方程解的数量。",
    ],
)
def test_arbitrary_unsolved_problem_is_reference_only_instead_of_mathematical_draft(
    stem,
):
    source = parsed("1. " + stem)
    pack = project_catalog(source)[0]
    assert pack["problem"]["stem"] == "1. " + stem
    assert pack["status"] == "reference_only"
    assert pack["source_answer"]["value"] is None
    assert pack["source_solution"] == []
    assert pack["verification"]["result"] == "not_checked"
    assert "unsupported_problem" not in {issue["code"] for issue in pack["issues"]}


def test_natural_language_explicit_answer_is_preserved_from_source_label():
    source = parsed(
        "1. 沿400米跑道走一圈回到起点，路程和位移大小是多少？\n\n答案：路程400米，位移大小为0。"
    )
    pack = project_catalog(source)[0]
    assert pack["source_answer"]["value"] == "路程400米，位移大小为0。"
    assert pack["source_solution"][0]["text"].startswith("答案：")
    assert pack["source_solution_steps"] == []


def test_unresolved_source_is_draft_but_unrelated_exercise_is_not_blocked():
    source = parsed("1. 比较速度和速率。\n\n2. 解释参考系。")
    source["exercises"][0]["source_block_ids"].append("missing")
    packs = project_catalog(source)
    assert packs[0]["status"] == "draft"
    assert packs[1]["status"] == "reference_only"
    assert "unresolved_source" in {issue["code"] for issue in packs[0]["issues"]}
    assert all(ref["block_id"] != "missing" for ref in packs[0]["source_refs"])
    verify_catalog_pack(packs[0], source)


def test_blocking_issue_is_scoped_to_the_affected_original_block():
    source = parsed("1. 比较速度和速率。\n\n2. 解释参考系。")
    source["issues"].append(
        {
            "code": "ambiguous_problem_boundary",
            "severity": "blocking",
            "block_id": source["exercises"][0]["source_block_ids"][0],
        }
    )
    packs = project_catalog(source)
    assert packs[0]["status"] == "draft"
    assert packs[1]["status"] == "reference_only"


def test_hash_and_document_version_are_checked_separately_from_math():
    source = parsed()
    pack = project_catalog(source)[0]
    forged = deepcopy(pack)
    forged["source_answer"]["value"] = "another answer"
    with pytest.raises(ValueError, match="哈希"):
        verify_catalog_pack(forged, source)
    source["document"]["content_hash"] = "changed-original"
    with pytest.raises(ValueError, match="教材来源或版本"):
        verify_catalog_pack(pack, source)


def test_rehashed_reference_to_another_document_is_still_rejected():
    source = parsed()
    pack = project_catalog(source)[0]
    pack["source_refs"][0]["document_id"] = "foreign-document"
    pack["hash"] = catalog_hash(
        {key: value for key, value in pack.items() if key != "hash"}
    )
    with pytest.raises(ValueError, match="跨文档"):
        verify_catalog_pack(pack, source)


def test_legacy_program_derived_archive_is_read_without_relabeling_or_recomputing():
    source = parsed()
    pack = project_catalog(source)[0]
    pack["version"] = "teaching-pack-v2"
    pack["provenance"]["origin"] = "rule_based_analysis"
    pack["verification"] = {
        "result": "correct",
        "method": "legacy_exact_rational_solution_equivalence",
    }
    pack["solution_paths"] = [
        {
            "id": "old-path",
            "label": "旧程序推导档案",
            "steps": [{"equation": "x=999"}],
            "requirements": {},
            "verification": {"result": "correct", "method": "legacy_math"},
        }
    ]
    pack["hash"] = catalog_hash(
        {key: value for key, value in pack.items() if key != "hash"}
    )
    verify_catalog_pack(pack, source)
    assert pack["solution_paths"][0]["steps"][0]["equation"] == "x=999"
    assert pack["provenance"]["origin"] == "rule_based_analysis"


def test_real_official_curated_chapter_keeps_original_answers_and_all_source_locations():
    from app.domains.materials.parsing.parser import parse_file

    target = (
        Path(__file__).resolve().parents[2]
        / "data/raw/textbooks/openstax_elementary_algebra_2e/linear_equations_curated_excerpt.html"
    )
    if not target.exists():
        pytest.skip("公开教材节选尚未获取。")
    source = parse_file(target, source_kind="curated_excerpt")
    packs = project_catalog(source)
    assert len(packs) == 12
    assert all(pack["status"] in {"ready", "reference_only"} for pack in packs)
    assert all("derived_answer" not in pack for pack in packs)
    source_answers = {
        exercise["id"]: exercise["answer"] for exercise in source["exercises"]
    }
    assert all(
        pack["source_answer"]["value"] == source_answers[pack["question_id"]]
        for pack in packs
    )
    assert (
        next(pack for pack in packs if pack["problem"]["number"] == "2.31")[
            "source_answer"
        ]["value"]
        is None
    )
    assert (
        next(pack for pack in packs if pack["problem"]["number"] == "2.42")[
            "source_answer"
        ]["value"]
        == r"x=-\frac{9}{2}"
    )
    assert (
        len({ref["source_uri"] for pack in packs for ref in pack["source_refs"]}) == 2
    )
    for pack in packs:
        verify_catalog_pack(pack, source)
