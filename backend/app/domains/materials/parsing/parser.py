"""统一教材结构协议，格式适配器和教学对象组装彼此独立。"""

from hashlib import sha256
from pathlib import Path
import re

from .errors import UnsupportedDocument
from .html_parser import extract_html
from .boundaries import split_solution_marker

PARSER_VERSION = "rules-1.3"
MAX_DOCUMENT_BYTES = 12 * 1024 * 1024
FORMAT_ADAPTERS = {}


def register_format_adapter(format: str, extractor):
    """扩展格式时复用同一组装协议；注册发生在服务启动配置阶段。"""
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", format) or not callable(extractor):
        raise ValueError("教材适配器名称或提取函数无效。")
    FORMAT_ADAPTERS[format] = extractor


def stable_id(prefix, *parts):
    raw = "\x1f".join(str(p) for p in parts).encode("utf-8")
    return prefix + "_" + sha256(raw).hexdigest()[:24]


def _inline_maths(text):
    # 只提取明确的数学定界符；不删除公式内部空白和括号结构。
    pattern = r"\$\$(.+?)\$\$|(?<!\\)\$([^$\n]+?)(?<!\\)\$|\\\((.+?)\\\)|\\\[(.+?)\\\]"
    return [
        {
            "format": "latex",
            "raw": m.group(0),
            "latex": next(x for x in m.groups() if x is not None).strip(),
            "status": "parsed",
        }
        for m in re.finditer(pattern, text, re.S)
    ]


def _equations(text, maths):
    result = []
    for item in maths:
        value = item.get("latex")
        if value and "=" in value:
            value = value.strip().rstrip("。.,，;；")
            if value not in result:
                result.append(value)
    if not maths:
        # 无定界符时只接纳一行独立等式，避免把叙述、HTML属性或图片描述误当公式。
        for line in text.splitlines():
            value = re.sub(r"^(?:解答?|答案|答)\s*[：:]\s*", "", line.strip())
            value = re.sub(
                r"^(?:例(?:题)?|Example)\s*\d+(?:\.\d+)*[.、:：\s]*",
                "",
                value,
                flags=re.I,
            )
            value = re.sub(r"^\d+[.、．)）]\s*", "", value)
            value = re.sub(r"^(?:解方程|求解|Solve)\s*[：:]\s*", "", value, flags=re.I)
            value = value.rstrip("。;；")
            if "=" in value and re.fullmatch(
                r"[0-9a-zA-Zα-ωΑ-Ω+\-−×÷*/().（）\s=\\{}_^]+", value
            ):
                if value not in result:
                    result.append(value)
    return [value for value in result if all(part.strip() for part in value.split("="))]


def _markdown(content):
    units, issues, pending, start = [], [], [], 1
    fenced, display = False, False

    def flush():
        if not pending:
            return
        text = "\n".join(pending).strip()
        units.append(
            {
                "text": text,
                "source": {
                    "page": None,
                    "bbox": None,
                    "locator": f"markdown:line:{start}",
                    "line": start,
                },
                "maths": _inline_maths(text),
                "images": [],
            }
        )
        pending.clear()

    for line_no, line in enumerate(content.splitlines(), 1):
        if re.match(r"\s*```|\s*~~~", line):
            flush()
            fenced = not fenced
            if fenced:
                issues.append(
                    {
                        "code": "code_block_ignored",
                        "severity": "info",
                        "message": "代码块未作为教材公式或习题执行。",
                        "source": {
                            "locator": f"markdown:line:{line_no}",
                            "page": None,
                            "bbox": None,
                        },
                    }
                )
            continue
        if fenced:
            continue
        split = (
            split_solution_marker(line)
            if not display and line.count("$$") % 2 == 0
            else None
        )
        if split:
            flush()
            before, after, offset = split
            for text, column, kind in ((before, 0, None), (after, offset, "solution")):
                units.append(
                    {
                        "text": text,
                        "kind": kind,
                        "source": {
                            "page": None,
                            "bbox": None,
                            "locator": f"markdown:line:{line_no}:column:{column}",
                            "line": line_no,
                            "column": column,
                            "end_column": column + len(text),
                        },
                        "maths": _inline_maths(text),
                        "images": [],
                    }
                )
            continue
        if line.count("$$") % 2:
            display = not display
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line) if not display else None
        image = re.fullmatch(r"\s*!\[([^]]*)\]\(([^)]+)\)\s*", line)
        if heading:
            flush()
            units.append(
                {
                    "text": heading[2],
                    "kind": "heading",
                    "heading_level": len(heading[1]),
                    "source": {
                        "page": None,
                        "bbox": None,
                        "locator": f"markdown:line:{line_no}",
                        "line": line_no,
                    },
                    "maths": [],
                    "images": [],
                }
            )
        elif image:
            flush()
            source = {
                "page": None,
                "bbox": None,
                "locator": f"markdown:line:{line_no}",
                "line": line_no,
            }
            units.append(
                {
                    "text": "[图像]",
                    "kind": "figure",
                    "source": source,
                    "maths": [],
                    "images": [
                        {
                            "src": image[2],
                            "source_description": image[1],
                            "status": "needs_review",
                            "source": source,
                        }
                    ],
                }
            )
            issues.append(
                {
                    "code": "image_content_unread",
                    "severity": "review",
                    "message": "图中内容未识别，替代描述仅作为来源描述保存。",
                    "source": source,
                    "fallback": "visual_or_human_review",
                }
            )
        elif not line.strip() and not display:
            flush()
        else:
            # 数学逐行作答和题号是明确边界，不把多道习题粘成同一段。
            if (
                pending
                and not display
                and (
                    _infer(line)[0] != "paragraph"
                    or _equations(line, _inline_maths(line))
                )
            ):
                flush()
            if not pending:
                start = line_no
            pending.append(line)
    flush()
    if fenced or display:
        issues.append(
            {
                "code": "unclosed_delimiter",
                "severity": "review",
                "message": "存在未闭合的代码块或数学定界符。",
                "source": {"locator": "markdown:end", "page": None, "bbox": None},
            }
        )
    return units, issues


def _infer(text):
    value = text.strip()
    match = re.match(
        r"^(?:例(?:题)?|Example)\s*([0-9]+(?:\.[0-9]+)*|[一二三四五六七八九十]+)[.、:：\s]*(.*)",
        value,
        re.I | re.S,
    )
    if match:
        return "example", match[1]
    match = re.match(
        r"^(?:(?:习题|练习|Exercise)\s*)?([0-9]+(?:\.[0-9]+)*)[.、．)）]\s*(.+)",
        value,
        re.I | re.S,
    )
    if match:
        return "exercise", match[1]
    if re.match(
        r"^(?:解答?|解析|答案|分析|答|Solution|Answer)\s*(?:[：:]|$)", value, re.I
    ):
        return "solution", None
    if re.match(r"^(?:定义|概念|Definition)\s*[：:]", value, re.I):
        return "definition", None
    if re.match(r"^(?:定理|法则|性质|公式|Rule|Theorem)\s*[：:]", value, re.I):
        return "rule", None
    if re.match(r"^(?:技巧|方法|小结|归纳|注意|Technique|How to)\s*[：:]", value, re.I):
        return "technique", None
    return "paragraph", None


def _assemble(units, issues, document):
    chapters, blocks, exercises, by_group, by_number = [], [], [], {}, {}
    current = None
    current_group = None
    solution_mode = False
    chapter_id = stable_id("ch", document["id"], "default")
    chapters.append({"id": chapter_id, "title": document["title"], "ordinal": 1})
    for ordinal, unit in enumerate(units, 1):
        text = unit["text"].strip()
        inferred, parsed_number = _infer(text)
        kind = unit.get("kind") or inferred
        source = {
            "document_id": document["id"],
            "source_uri": document["source_uri"],
            "page": None,
            "bbox": None,
            **unit["source"],
        }
        number = str(unit.get("number") or parsed_number or "")
        group = unit.get("group")
        if current_group and group != current_group:
            current, solution_mode, current_group = None, False, None
        if kind == "heading":
            if (unit.get("heading_level") or 2) <= 2:
                if not blocks and len(chapters) == 1:
                    chapters.clear()
                chapter_id = stable_id("ch", document["id"], source["locator"], text)
                chapters.append(
                    {"id": chapter_id, "title": text, "ordinal": len(chapters) + 1}
                )
            current, solution_mode = None, False
        maths = unit.get("maths", []) + _inline_maths(text)
        # HTML MathML 渲染出的 $...$ 已有结构记录，不重复收录。
        dedup = []
        for item in maths:
            if not any(x.get("latex") == item.get("latex") for x in dedup):
                dedup.append(item)
        equations = [] if unit.get("layout_uncertain") else _equations(text, dedup)
        block_id = stable_id("block", document["id"], source["locator"], ordinal, kind)
        block = {
            "id": block_id,
            "chapter_id": chapter_id,
            "kind": kind,
            "ordinal": ordinal,
            "text": text,
            "source": source,
            "equations": equations,
            "maths": dedup,
        }
        if unit.get("images"):
            block["images"] = unit["images"]
        if unit.get("native_words"):
            block["native_words"] = unit["native_words"]
        if unit.get("layout_uncertain"):
            block["needs_review"] = True
        if unit.get("association_unknown"):
            block["association_status"] = "needs_review"
        if kind in {"example", "exercise"}:
            key = group or f"{chapter_id}:{kind}:{number or ordinal}"
            if key in by_group:
                current = by_group[key]
                current_group = group
                # 同一个显式题目容器的题干跨段延续；解答不因新段落变回题干。
                kind = "solution" if solution_mode else "paragraph"
                block["kind"] = kind
            else:
                current = {
                    "id": stable_id("exercise", document["id"], key),
                    "chapter_id": chapter_id,
                    "number": number or str(len(exercises) + 1),
                    "kind": kind,
                    "stem": text,
                    "equation": equations[0] if equations else None,
                    "solution_steps": [],
                    "answer": None,
                    "source_block_ids": [],
                    "solution_block_ids": [],
                }
                exercises.append(current)
                by_group[key] = current
                current_group = group
                by_number.setdefault((chapter_id, current["number"]), []).append(
                    current
                )
                solution_mode = False
        if kind == "solution" or unit.get("solution_context"):
            reference = unit.get("reference")
            if reference:
                # 显式 data-for 优先；不能唯一匹配时禁止套用“上一题”。
                ref = reference.lstrip("#")
                target = by_group.get("#" + ref) or by_group.get(ref)
                candidates = by_number.get((chapter_id, ref), [])
                current = target or (candidates[0] if len(candidates) == 1 else None)
            if current:
                solution_mode = True
            else:
                issues.append(
                    {
                        "code": "orphan_solution",
                        "severity": "review",
                        "message": "解答未能唯一关联到题目，已保留原文，未绑定其他题。",
                        "source": source,
                    }
                )
        elif kind not in {"paragraph", "figure", "formula", "example", "exercise"}:
            current, solution_mode = None, False
        if (
            current
            and not unit.get("association_unknown")
            and kind
            in {"example", "exercise", "paragraph", "figure", "formula", "solution"}
        ):
            block["parent_id"] = current["id"]
            block["exercise_key"] = current["number"]
            if solution_mode:
                if kind not in {"figure", "formula"}:
                    block["kind"] = "solution"
                current["solution_block_ids"].append(block_id)
                for equation in equations:
                    if equation not in current["solution_steps"]:
                        current["solution_steps"].append(equation)
                    # 这里只标记来源中的显式孤立未知数，正确性留给题型核验器。
                    if (
                        re.match(r"^\s*[a-zA-Z]\s*=", equation)
                        and equation.count("=") == 1
                    ):
                        current["answer"] = equation
                        current["answer_source"] = {
                            "kind": "source_text",
                            "block_id": block_id,
                        }
            else:
                if current["source_block_ids"]:
                    current["stem"] += "\n" + text
                current["source_block_ids"].append(block_id)
                if not current["equation"] and equations:
                    current["equation"] = equations[0]
        blocks.append(block)
    for exercise in exercises:
        if not exercise["solution_block_ids"]:
            issues.append(
                {
                    "code": "missing_solution",
                    "severity": "info",
                    "exercise_id": exercise["id"],
                    "message": "来源中没有已关联的解答，未生成或猜测答案。",
                }
            )
    fallback = [
        {
            "id": stable_id("region", document["id"], i, item.get("source", {})),
            "reason": item["code"],
            "source": item.get("source"),
            "status": "pending",
            "requested_method": item.get("fallback", "human_review"),
            "attempted": False,
        }
        for i, item in enumerate(issues)
        if item.get("severity") == "review"
    ]
    return {
        "schema_version": "textbook-v1",
        "document": document,
        "chapters": chapters,
        "blocks": blocks,
        "exercises": exercises,
        "issues": issues,
        "fallback_regions": fallback,
        "needs_review": bool(fallback),
        "stats": {
            "chapter_count": len(chapters),
            "block_count": len(blocks),
            "exercise_count": len(exercises),
            "formula_count": sum(len(b["maths"]) for b in blocks),
            "issue_count": len(issues),
            "fallback_region_count": len(fallback),
            "visual_model_calls": 0,
            "ocr_calls": 0,
        },
    }


def parse_document(
    content: str | bytes,
    *,
    format: str,
    title: str,
    edition: str = "",
    source_uri: str = "",
    source_kind: str = "uploaded",
    document_id: str | None = None,
) -> dict:
    """解析显式提供的内容；来源类别与文件格式分别保存，不读取远端 URL。"""
    raw = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise UnsupportedDocument("教材超过 12 MiB，请按章节分批导入。")
    file_format = format.lower().lstrip(".")
    file_format = {"md": "markdown", "htm": "html"}.get(file_format, file_format)
    digest = sha256(raw).hexdigest()
    document = {
        "id": document_id or stable_id("doc", digest),
        "title": title,
        "edition": edition,
        "source_uri": source_uri,
        "source_kind": source_kind,
        "format": file_format,
        "content_hash": digest,
        "parser_version": PARSER_VERSION,
        "extraction_method": {
            "html": "html_structure_rules",
            "markdown": "markdown_structure_rules",
            "pdf": "pdf_native_layout_rules",
        }.get(file_format),
    }
    if file_format == "pdf":
        from .pdf_parser import extract_pdf

        units, issues = extract_pdf(raw)
    elif file_format in {"html", "markdown"}:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise UnsupportedDocument(
                "文本教材必须使用 UTF-8 编码，未执行有损解码。"
            ) from exc
        units, issues = extract_html(text) if file_format == "html" else _markdown(text)
    elif file_format in FORMAT_ADAPTERS:
        units, issues = FORMAT_ADAPTERS[file_format](raw)
        document["extraction_method"] = "registered_format_adapter"
    else:
        raise UnsupportedDocument(f"尚未支持教材格式：{file_format}。")
    return _assemble(units, issues, document)


def parse_file(path: str | Path, **metadata) -> dict:
    target = Path(path)
    return parse_document(
        target.read_bytes(),
        format=metadata.pop("format", target.suffix),
        title=metadata.pop("title", target.stem),
        source_uri=metadata.pop("source_uri", target.resolve().as_uri()),
        **metadata,
    )
