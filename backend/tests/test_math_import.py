"""验证数学元数据中容易损坏的官方标识、图片和答案隔离。"""

import json

from app.application.commands.courses.import_math import prepare_course, sanitize_content


def test_sanitize_content_keeps_latex_local_images_and_removes_executable_html(
    tmp_path,
):
    (tmp_path / "question_20-image_0.png").write_bytes(b"image")
    value = r'<p onclick="bad()">求 $$a<{}b$$ 的值 question_20-image_0</p><script>secret()</script><img src="https://example.org/x.png" onerror="bad()">'
    result = sanitize_content(value, tmp_path)
    assert "$$a<{}b$$" in result["markdown"]
    assert (
        sanitize_content("$$x<y$$ 且 $$x>0$$", tmp_path)["markdown"]
        == "$$x<y$$ 且 $$x>0$$"
    )
    assert "/api/assets/math/question_20-image_0.png" in result["markdown"]
    assert result["images"] == ["question_20-image_0.png"]
    assert result["missing_images"] == []
    assert result["external_image_count"] == 1
    assert "onclick" not in result["html"] and "secret" not in result["html"]
    assert "https://" not in result["html"] and "onerror" not in result["html"]


def test_missing_image_is_recorded_without_path_traversal(tmp_path):
    result = sanitize_content(
        'question_7-image_0 <img src="../question_8-image_0.png">', tmp_path
    )
    assert result["images"] == ["question_7-image_0.png"]
    assert result["missing_images"] == ["question_7-image_0.png"]
    assert "../" not in result["html"]


def test_prepare_course_preserves_official_ids_paths_and_private_answers(tmp_path):
    (tmp_path / "images").mkdir()
    nodes = {"0": "几何思想", "1": " 几何思想", "2": "数学"}
    rows = {
        "7": {
            "content": "计算面积 question_7-image_0",
            "kc_routes": ["数学----几何思想"],
            "answer": ["答案哨兵"],
            "analysis": "解析哨兵",
            "type": "填空",
            "options": {},
        },
        "8": {
            "content": "选择长度",
            "kc_routes": ["数学---- 几何思想"],
            "answer": ["A"],
            "analysis": "其他解析",
            "type": "单选",
            "options": {"A": "$$1$$", "B": "$$2$$"},
        },
    }
    (tmp_path / "questions.json").write_text(json.dumps(rows), encoding="utf-8")
    (tmp_path / "kc_routes_map.json").write_text(json.dumps(nodes), encoding="utf-8")
    bundle = prepare_course(tmp_path)
    report = bundle["report"]
    assert report["hierarchy_nodes"] == 3
    assert report["leaf_concept_ids"] == 2
    assert report["leaf_names_exact"] == 2 and report["leaf_names_stripped"] == 1
    assert bundle["questions"][0]["concept_ids"] == ["math:c0"]
    assert bundle["questions"][1]["concept_ids"] == ["math:c1"]
    assert bundle["questions"][0]["validation"]["knowledge_route_ids"] == [["2", "0"]]
    assert bundle["questions"][0]["answer"] == ["答案哨兵"]
    assert bundle["questions"][0]["explanation"] == "解析哨兵"
    assert "答案哨兵" not in json.dumps(bundle["materials"], ensure_ascii=False)
    assert "解析哨兵" not in json.dumps(bundle["materials"], ensure_ascii=False)
    assert "解析哨兵" not in json.dumps(
        bundle["questions"][0]["validation"], ensure_ascii=False
    )
    assert {edge["relation_type"] for edge in bundle["relations"]} == {"HAS_CHILD"}
    assert report["missing_figure_question_ids"] == ["math:7"]
    assert report["historical_interaction_files"] == 0


def test_incomplete_or_duplicate_source_options_are_retained_but_quarantined(tmp_path):
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "question_1-image_0.png").write_bytes(b"image")
    rows = {}
    variants = [
        {"A": "选我即可查看答案"},
        {"A": "question_1-image_0", "B": "question_1-image_0"},
        {"A": "$$3$$", "B": "$$3$$", "C": "$$4$$"},
    ]
    for index, options in enumerate(variants):
        rows[str(index)] = {
            "content": "原始题目",
            "kc_routes": ["数学----计算"],
            "answer": ["A"],
            "analysis": "原始解析",
            "type": "单选",
            "options": options,
        }
    (tmp_path / "questions.json").write_text(json.dumps(rows), encoding="utf-8")
    (tmp_path / "kc_routes_map.json").write_text(
        json.dumps({"0": "数学", "1": "计算"}), encoding="utf-8"
    )
    bundle = prepare_course(tmp_path)
    assert len(bundle["questions"]) == 3
    assert [q["validation"]["availability"] for q in bundle["questions"]] == [
        "insufficient_options",
        "ambiguous_options",
        "duplicate_options",
    ]
    assert bundle["report"]["independent_practice_questions"] == 0
    assert (
        bundle["questions"][1]["options"]["A"] == bundle["questions"][1]["options"]["B"]
    )


def test_multiple_fill_blanks_are_ordered_not_single_blank_aliases(tmp_path):
    rows = {
        "7644": {
            "content": "两空分别填写",
            "kc_routes": ["数学"],
            "answer": ["$$60$$", "$$20$$"],
            "analysis": "两项有序答案",
            "type": "填空",
            "options": {},
        }
    }
    (tmp_path / "questions.json").write_text(json.dumps(rows), encoding="utf-8")
    (tmp_path / "kc_routes_map.json").write_text(
        json.dumps({"0": "数学"}), encoding="utf-8"
    )
    question = prepare_course(tmp_path)["questions"][0]
    assert question["answer"] == {"1": ["$$60$$"], "2": ["$$20$$"]}
    assert question["validation"]["blank_count"] == 2
    assert question["validation"]["answer_structure"] == "ordered_blanks"
