"""验证教材解析保留数学结构、原文定位及不确定性，而不是只验输出字段。"""

import json

import pytest

from app.domains.materials.parsing.parser import UnsupportedDocument, parse_document
from app.domains.materials.parsing.mathml import UnsupportedMathML, mathml_to_latex


def parse(content, **kwargs):
    return parse_document(
        content,
        format=kwargs.pop("format", "html"),
        title="一元一次方程",
        source_uri="https://example.org/chapter",
        source_kind="self_authored",
        **kwargs,
    )


def test_html_nested_example_and_solution_do_not_mix_answers_or_execute_scripts():
    value = """<h1>一元一次方程</h1>
    <script>danger(); x=999</script>
    <div data-type="example" id="q1" data-number="1">
      <div data-type="exercise"><div data-type="problem"><p>解方程 $2(x-3)=8$。</p><p>请说明每一步。</p></div>
      <details data-type="solution"><h4 data-type="solution-title">Solution</h4><p>$2x-6=8$</p><p>$x=7$</p></details></div>
    </div>
    <p>下面介绍新的方法。</p>
    <div data-type="example" id="q2" data-number="2"><p>解方程 $x+1=3$。</p><div data-type="solution"><p>$x=2$</p></div></div>"""
    result = parse(value)
    first, second = result["exercises"]
    assert first["kind"] == "example"
    assert first["stem"] == "解方程 $2(x-3)=8$。\n请说明每一步。"
    assert first["solution_steps"] == ["2x-6=8", "x=7"]
    assert second["solution_steps"] == ["x=2"]
    assert first["answer"] == "x=7"
    assert not any(
        "danger" in b["text"] or "下面介绍" in first["stem"] for b in result["blocks"]
    )
    ids = {b["id"] for b in result["blocks"]}
    assert set(first["source_block_ids"] + first["solution_block_ids"]) <= ids
    assert "Solution" not in [b["text"] for b in result["blocks"]]


def test_mathml_fractions_keep_structure_and_semantics_annotations_are_not_duplicated():
    fraction = "<math><semantics><mrow><mi>x</mi><mo>=</mo><mfrac><mn>1</mn><mn>23</mn></mfrac></mrow><annotation-xml><mrow><mi>x</mi><mo>=</mo><mn>WRONG</mn></mrow></annotation-xml></semantics></math>"
    value = f'<p data-kind="exercise" data-number="1">{fraction}</p>'
    first = parse(value)
    assert first["exercises"][0]["equation"] == r"x=\frac{1}{23}"
    assert len(first["blocks"][0]["maths"]) == 1
    assert "WRONG" not in first["blocks"][0]["text"]
    other = mathml_to_latex("<math><mfrac><mn>12</mn><mn>3</mn></mfrac></math>")
    assert other == r"\frac{12}{3}"
    assert other != first["blocks"][0]["equations"][0]
    assert "annotation-xml" in first["blocks"][0]["maths"][0]["raw"]


def test_unknown_mathml_and_images_become_auditable_pending_regions():
    result = parse(
        '<div data-kind="example" data-number="1"><p>题干 <math><mtable><mtr><mtd><mi>x</mi></mtd></mtr></mtable></math></p><div data-kind="solution"><img src="q.png" alt="x=999"/></div></div>'
    )
    item = result["exercises"][0]
    assert item["equation"] is None
    assert item["answer"] is None
    assert item["solution_steps"] == []
    assert result["needs_review"]
    assert {i["reason"] for i in result["fallback_regions"]} == {
        "unsupported_mathml",
        "image_content_unread",
    }
    assert all(
        r["status"] == "pending" and not r["attempted"]
        for r in result["fallback_regions"]
    )
    assert result["stats"]["visual_model_calls"] == result["stats"]["ocr_calls"] == 0
    image = next(b for b in result["blocks"] if b.get("images"))["images"][0]
    assert image["source_description"] == "x=999"
    assert "x=999" not in "\n".join(b["text"] for b in result["blocks"])


def test_explicit_mismatched_solution_reference_does_not_attach_to_previous_question():
    result = parse(
        '<p data-kind="exercise" data-number="1">$x+1=3$</p><p data-kind="solution" data-for="missing">$x=999$</p>'
    )
    assert result["exercises"][0]["solution_steps"] == []
    assert any(i["code"] == "orphan_solution" for i in result["issues"])


@pytest.mark.parametrize(
    "wrapped",
    [
        "<span data-kind='solution'>答案：$x=4$</span>",
        "<em><strong><span data-type='solution'>答案：$x=4$</span></strong></em>",
        "<span class='solution'>答案：$x=4$</span>",
    ],
)
def test_nested_inline_solution_is_not_rendered_into_public_problem_stem(wrapped):
    value = (
        f"<div data-kind='exercise' id='q'><p id='p'>求x：$2x=8$。{wrapped}</p></div>"
    )
    result = parse(value)
    exercise = result["exercises"][0]
    assert exercise["stem"] == "求x：$2x=8$。"
    assert exercise["answer"] == "x=4"
    assert exercise["solution_steps"] == ["x=4"]
    blocks = {block["id"]: block for block in result["blocks"]}
    assert exercise["source_block_ids"] and exercise["solution_block_ids"]
    assert all(
        "答案" not in blocks[key]["text"] for key in exercise["source_block_ids"]
    )
    assert all(
        blocks[key]["source"]["line"] == 1
        for key in exercise["source_block_ids"] + exercise["solution_block_ids"]
    )
    assert all(
        blocks[key]["source"]["source_uri"] == "https://example.org/chapter"
        for key in blocks
    )


@pytest.mark.parametrize(
    "label", ["答案：", "参考答案：", "解答：", "解析：", "Answer:"]
)
def test_same_line_markdown_explicit_solution_marker_is_split_with_original_column(
    label,
):
    value = f"1. 求x：$2x=8$。 {label}$x=4$"
    result = parse(value, format="markdown")
    exercise = result["exercises"][0]
    assert exercise["stem"] == "1. 求x：$2x=8$。"
    assert exercise["answer"] == "x=4"
    blocks = {block["id"]: block for block in result["blocks"]}
    solution = blocks[exercise["solution_block_ids"][0]]
    assert solution["text"] == label + "$x=4$"
    assert solution["source"]["line"] == 1
    assert solution["source"]["column"] == value.index(label)
    assert solution["source"]["locator"].endswith(f"column:{value.index(label)}")


def test_solution_markers_inside_math_or_code_do_not_erase_question_conditions():
    value = r"1. 解释 $\text{答案：某个符号}$ 和 `答案：占位`，再写出 $x=4$ 的含义。"
    result = parse(value, format="markdown")
    assert result["exercises"][0]["stem"] == value
    assert result["exercises"][0]["solution_block_ids"] == []


def test_explicit_html_text_marker_splits_source_without_matching_the_answer_value():
    value = (
        "<p data-kind='exercise' id='same-line'>已知$x=4$，求$2x$。 答案：$2x=8$</p>"
    )
    result = parse(value)
    exercise = result["exercises"][0]
    assert exercise["stem"] == "已知$x=4$，求$2x$。"
    assert "x=4" in exercise["stem"]
    solution = next(
        block
        for block in result["blocks"]
        if block["id"] in exercise["solution_block_ids"]
    )
    assert solution["text"] == "答案：$2x=8$"
    assert solution["source"]["locator"] == "#same-line"
    assert solution["source"]["rendered_text_span"][0] > 0


@pytest.mark.parametrize("tail", ["答案：$2x=8$", "补充条件不变。 答案：$2x=8$"])
def test_nested_ordinary_inline_fragment_cannot_bypass_solution_marker_boundary(tail):
    value = f"<div data-kind='exercise'><p id='mixed'>已知$x=4$，求$2x$。<span data-kind='paragraph'>请说明理由。</span>{tail}</p></div>"
    result = parse(value)
    exercise = result["exercises"][0]
    assert "x=4" in exercise["stem"]
    assert "请说明理由" in exercise["stem"]
    assert "答案" not in exercise["stem"] and "2x=8" not in exercise["stem"]
    blocks = {block["id"]: block for block in result["blocks"]}
    assert exercise["solution_block_ids"]
    assert any("2x=8" in blocks[key]["text"] for key in exercise["solution_block_ids"])
    assert all(
        blocks[key]["source"]["line"] == 1
        for key in exercise["source_block_ids"] + exercise["solution_block_ids"]
    )


def test_origin_format_stable_ids_and_per_fragment_source_are_separate():
    value = '<section data-source-url="https://publisher.org/original" data-source-path="raw/2_3.html"><p data-kind="exercise" id="q">$x+1=3$</p></section>'
    first, second = parse(value), parse(value)
    assert first == second
    assert first["document"]["source_kind"] == "self_authored"
    assert first["document"]["format"] == "html"
    assert (
        first["blocks"][0]["source"]["source_uri"] == "https://publisher.org/original"
    )
    assert first["blocks"][0]["source"]["locator"] == "#q"
    assert (
        first["document"]["content_hash"]
        != parse(value + " ")["document"]["content_hash"]
    )
    json.dumps(first)


def test_real_publisher_section_urls_do_not_collapse_to_the_document_root():
    uri23 = "https://openstax.org/books/elementary-algebra-2e/pages/2-3-solve-equations-with-variables-and-constants-on-both-sides"
    uri24 = "https://openstax.org/books/elementary-algebra-2e/pages/2-4-use-a-general-strategy-to-solve-linear-equations"
    value = f"""<section data-source-url="{uri23}" data-source-path="2_3.html"><div data-type="example" data-number="2.27"><p>$7x+8=-13$</p><details data-type="solution"><p>$x=-3$</p></details></div></section>
    <section data-source-url="{uri24}" data-source-path="2_4.html"><div data-type="example" data-number="2.37"><p>$-6(x+3)=24$</p><details data-type="solution"><img src="answer.png" alt="unverified solution description"/></details></div></section>"""
    result = parse_document(
        value,
        format="html",
        title="Official excerpts",
        source_uri=uri23,
        source_kind="curated_excerpt",
    )
    blocks = {block["id"]: block for block in result["blocks"]}
    first, second = result["exercises"]
    assert {
        blocks[key]["source"]["source_uri"]
        for key in first["source_block_ids"] + first["solution_block_ids"]
    } == {uri23}
    assert {
        blocks[key]["source"]["source_uri"]
        for key in second["source_block_ids"] + second["solution_block_ids"]
    } == {uri24}
    image_block = next(
        blocks[key] for key in second["solution_block_ids"] if blocks[key].get("images")
    )
    assert image_block["images"][0]["source"]["source_uri"] == uri24
    assert (
        next(
            issue
            for issue in result["issues"]
            if issue["code"] == "image_content_unread"
        )["source"]["source_uri"]
        == uri24
    )
    assert (
        next(
            region
            for region in result["fallback_regions"]
            if region["reason"] == "image_content_unread"
        )["source"]["original_path"]
        == "2_4.html"
    )
    assert "[图像]" not in second["stem"]


def test_markdown_multiple_questions_steps_and_figures_remain_distinct():
    value = """# 一元一次方程

定义：只含一个未知数，未知数次数为一的方程。

例1 解方程 $-2(x-3)=8$。

解：
$-2x+6=8$
$x=-1$

![图中条件尚未识别](figure.png)

2. $3x=12$

答案：$x=4$

```python
x=999
```
"""
    result = parse(value, format="markdown")
    first, second = result["exercises"]
    assert first["equation"] == "-2(x-3)=8"
    assert first["solution_steps"] == ["-2x+6=8", "x=-1"]
    assert second["equation"] == "3x=12"
    assert second["answer"] == "x=4"
    assert any(b["kind"] == "definition" for b in result["blocks"])
    assert all("999" not in b["text"] for b in result["blocks"])


def _pdf(streams):
    """构造可审计的最小原生 PDF，避免测试依赖额外生成库。"""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    kids = []
    for stream in streams:
        page_no = len(objects) + 1
        content_no = page_no + 1
        kids.append(f"{page_no} 0 R")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] /Resources << /Font << /F1 3 0 R >> >> /Contents {content_no} 0 R >>".encode()
        )
        data = stream.encode("ascii")
        objects.append(
            b"<< /Length "
            + str(len(data)).encode()
            + b" >>\nstream\n"
            + data
            + b"\nendstream"
        )
    objects[1] = (
        f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>".encode()
    )
    output, offsets = bytearray(b"%PDF-1.4\n"), [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    )
    return bytes(output)


def test_native_pdf_records_page_coordinates_and_filters_repeated_headers():
    pytest.importorskip("pdfplumber")
    data = _pdf(
        [
            "BT /F1 12 Tf 40 780 Td (Textbook Header 1) Tj 0 -80 Td (1. x+1=3) Tj ET",
            "BT /F1 12 Tf 40 780 Td (Textbook Header 2) Tj 0 -80 Td (2. 2x=8) Tj ET",
        ]
    )
    result = parse(data, format="pdf")
    assert [x["equation"] for x in result["exercises"]] == ["x+1=3", "2x=8"]
    assert not any("Header" in b["text"] for b in result["blocks"])
    assert {b["source"]["page"] for b in result["blocks"]} == {1, 2}
    assert all(
        len(b["source"]["bbox"]) == 4 and b["native_words"] for b in result["blocks"]
    )


def test_pdf_without_native_text_does_not_invent_ocr_output():
    pytest.importorskip("pdfplumber")
    result = parse(_pdf([""]), format="pdf")
    assert result["blocks"] == result["exercises"] == []
    assert result["fallback_regions"][0]["reason"] == "image_only_page"
    assert result["fallback_regions"][0]["source"]["page"] == 1
    assert result["stats"]["ocr_calls"] == 0


def test_native_pdf_two_columns_are_read_top_to_bottom_per_column():
    pytest.importorskip("pdfplumber")
    data = _pdf(
        [
            "BT /F1 12 Tf 40 700 Td (1. x+1=3) Tj 0 -20 Td (Solution:) Tj 0 -20 Td (x=2) Tj ET BT /F1 12 Tf 360 700 Td (2. 2x=8) Tj 0 -20 Td (Solution:) Tj 0 -20 Td (x=4) Tj ET"
        ]
    )
    result = parse(data, format="pdf")
    first, second = result["exercises"]
    assert first["equation"] == "x+1=3"
    assert first["answer"] == "x=2"
    assert second["equation"] == "2x=8"
    assert second["answer"] == "x=4"


def test_native_pdf_mixed_size_formula_does_not_become_verified_flat_text():
    pytest.importorskip("pdfplumber")
    data = _pdf(
        ["BT /F1 12 Tf 40 700 Td (1. x=) Tj ET BT /F1 7 Tf 70 700 Td (2) Tj ET"]
    )
    result = parse(data, format="pdf")
    assert result["exercises"][0]["equation"] is None
    assert any(i["code"] == "pdf_formula_layout_uncertain" for i in result["issues"])


def test_custom_format_adapter_reuses_the_shared_teaching_structure(monkeypatch):
    from app.domains.materials.parsing.parser import register_format_adapter
    from app.domains.materials.parsing import parser

    monkeypatch.setattr(parser, "FORMAT_ADAPTERS", {})
    register_format_adapter(
        "custom",
        lambda raw: ([{"text": "1. x+1=3", "source": {"locator": "custom:1"}}], []),
    )
    result = parse("source", format="custom")
    assert result["exercises"][0]["equation"] == "x+1=3"
    assert result["document"]["extraction_method"] == "registered_format_adapter"


@pytest.mark.parametrize(
    "content,fmt", [(b"\xff", "html"), ("text", "docx"), (b"broken", "pdf")]
)
def test_unsupported_or_invalid_document_fails_explicitly(content, fmt):
    with pytest.raises(UnsupportedDocument):
        parse(content, format=fmt)


def test_mathml_external_entity_and_unknown_structure_are_rejected():
    with pytest.raises(UnsupportedMathML):
        mathml_to_latex(
            '<!DOCTYPE math [<!ENTITY x SYSTEM "file:///secret">]><math>&x;</math>'
        )
    with pytest.raises(UnsupportedMathML):
        mathml_to_latex("<math><unknown>x</unknown></math>")
    assert (
        mathml_to_latex(
            "<math><semantics><mi>x</mi><annotation-xml><unknown>ignored</unknown></annotation-xml></semantics></math>"
        )
        == "x"
    )
