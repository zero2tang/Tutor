"""不执行脚本的 HTML 结构提取器，支持显式教学标签和原生 MathML。"""

from dataclasses import dataclass, field
from html.parser import HTMLParser
import re

from .mathml import UnsupportedMathML, mathml_to_latex, serialize_math_node
from .boundaries import split_solution_marker, starts_solution_marker


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    line: int = 1
    column: int = 0
    ordinal: int = 0


class TreeParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("document")
        self.stack = [self.root]
        self.count = 0

    def handle_starttag(self, tag, attrs):
        self.count += 1
        if self.count > 100_000 or len(self.stack) > 200:
            raise ValueError("HTML 结构超出安全解析范围。")
        node = Node(
            tag,
            dict(attrs),
            line=self.getpos()[0],
            column=self.getpos()[1],
            ordinal=self.count,
        )
        self.stack[-1].children.append(node)
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


SKIP = {"script", "style", "noscript", "template", "head", "nav", "footer"}
BLOCK_TAGS = {
    "p",
    "li",
    "tr",
    "blockquote",
    "pre",
    "figcaption",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
}
KINDS = {
    "heading",
    "paragraph",
    "definition",
    "rule",
    "example",
    "exercise",
    "solution",
    "technique",
    "figure",
    "formula",
}
ALIASES = {
    "problem": "exercise",
    "answer": "solution",
    "proof": "solution",
    "theorem": "rule",
    "note": "technique",
}


def semantic_kind(node):
    for attr in ("data-kind", "data-type"):
        candidate = node.attrs.get(attr, "").lower()
        candidate = ALIASES.get(candidate, candidate)
        if candidate in KINDS:
            return candidate
    tokens = re.split(r"\s+", node.attrs.get("class", "").lower())
    for candidate in (
        "solution",
        "example",
        "exercise",
        "definition",
        "theorem",
        "technique",
    ):
        if candidate in tokens:
            return ALIASES.get(candidate, candidate)
    return None


def extract_html(content: str):
    parser = TreeParser()
    parser.feed(content)
    units, issues = [], []

    def append_unit(unit):
        """所有段落与行内片段共用同一隐私边界，避免旁路分支把答案并入题干。"""
        text = unit["text"]
        if unit.get("kind") == "solution" or unit.get("solution_context"):
            units.append(unit)
            return
        if starts_solution_marker(text):
            units.append({**unit, "kind": "solution", "solution_context": True})
            return
        split = split_solution_marker(text)
        if not split:
            units.append(unit)
            return
        before, after, offset = split
        for part, start, end, is_solution in (
            (before, 0, offset, False),
            (after, offset, len(text), True),
        ):
            units.append(
                {
                    **unit,
                    "text": part,
                    "kind": "solution" if is_solution else unit.get("kind"),
                    "solution_context": is_solution,
                    "source": {**unit["source"], "rendered_text_span": [start, end]},
                    "maths": [
                        item
                        for item in unit.get("maths", [])
                        if (item.get("latex") and "$" + item["latex"] + "$" in part)
                        or (item.get("latex") is None and "[公式待复核]" in part)
                    ],
                }
            )

    def location(node, original_uri=None, original_path=None):
        ident = node.attrs.get("id")
        source = {
            "page": None,
            "bbox": None,
            "locator": f"#{ident}" if ident else f"html:{node.tag}:{node.ordinal}",
            "line": node.line,
            "column": node.column,
        }
        if original_uri:
            source["source_uri"] = original_uri
        if original_path:
            source["original_path"] = original_path
        return source

    def render(node, maths, images, original_uri=None, original_path=None):
        if isinstance(node, str):
            return node
        if node.tag in SKIP:
            return ""
        if node.tag == "math":
            raw = serialize_math_node(node)
            # 原生 LaTeX 注解优先，但只有明确的 annotation 才作为公式，而非任意 HTML 文本。
            annotation = None
            for item in node.children:
                if isinstance(item, Node) and item.tag == "semantics":
                    for child in item.children:
                        if (
                            isinstance(child, Node)
                            and child.tag == "annotation"
                            and "tex" in child.attrs.get("encoding", "").lower()
                        ):
                            annotation = "".join(
                                x for x in child.children if isinstance(x, str)
                            ).strip()
            try:
                latex = annotation or mathml_to_latex(raw)
                maths.append(
                    {"format": "mathml", "raw": raw, "latex": latex, "status": "parsed"}
                )
                return "$" + latex + "$"
            except UnsupportedMathML as exc:
                maths.append(
                    {
                        "format": "mathml",
                        "raw": raw,
                        "latex": None,
                        "status": "needs_review",
                    }
                )
                issues.append(
                    {
                        "code": "unsupported_mathml",
                        "severity": "review",
                        "message": str(exc),
                        "source": location(node, original_uri, original_path),
                        "fallback": "formula_review",
                    }
                )
                return "[公式待复核]"
        if node.tag == "img":
            image = {
                "src": node.attrs.get("src", ""),
                "source_description": node.attrs.get("alt", ""),
                "source": location(node, original_uri, original_path),
                "status": "needs_review",
            }
            images.append(image)
            issues.append(
                {
                    "code": "image_content_unread",
                    "severity": "review",
                    "message": "已保存原始图像及替代描述；图中数学条件尚未识别。",
                    "source": location(node, original_uri, original_path),
                    "fallback": "visual_or_human_review",
                }
            )
            return "[图像]"
        if node.tag == "br":
            return "\n"
        separator = " | " if node.tag == "tr" else ""
        return separator.join(
            render(c, maths, images, original_uri, original_path) for c in node.children
        )

    def has_blocks(node):
        return any(
            isinstance(c, Node)
            and (c.tag in BLOCK_TAGS or c.tag == "figure" or has_blocks(c))
            for c in node.children
        )

    def has_semantic_boundary(node):
        return any(
            isinstance(child, Node)
            and (semantic_kind(child) is not None or has_semantic_boundary(child))
            for child in node.children
        )

    def walk(
        node,
        inherited=None,
        group=None,
        number=None,
        reference=None,
        original_uri=None,
        original_path=None,
    ):
        if isinstance(node, str) or node.tag in SKIP:
            return
        own = semantic_kind(node)
        original_uri = node.attrs.get("data-source-url") or original_uri
        original_path = node.attrs.get("data-source-path") or original_path

        def source_location():
            source = location(node)
            if original_uri:
                source["source_uri"] = original_uri
            if original_path:
                source["original_path"] = original_path
            return source

        # OpenStax 在 example 内嵌 exercise/problem，仍然属于同一道例题。
        nested_problem = bool(group and own in {"example", "exercise"})
        if nested_problem:
            own = inherited
        if own in {"example", "exercise"} and not nested_problem:
            group = location(node)["locator"]
            number = node.attrs.get("data-number") or node.attrs.get("data-label")
            if not number:

                def find_number(item):
                    if isinstance(item, str):
                        return None
                    if "os-number" in item.attrs.get("class", "").split():
                        return "".join(
                            c for c in item.children if isinstance(c, str)
                        ).strip()
                    for child in item.children:
                        found = find_number(child)
                        if found:
                            return found
                    return None

                number = find_number(node)
        reference = (
            node.attrs.get("data-for") or node.attrs.get("data-exercise") or reference
        )
        active = own or inherited
        is_heading = bool(re.fullmatch(r"h[1-6]", node.tag))
        # 教学对象内部的标题通常是“例 1”，不把它升级为新的章节。
        if is_heading and group:
            label = render(node, [], [], original_uri, original_path).strip()
            if (
                node.attrs.get("data-role") == "label"
                or node.attrs.get("data-type") == "solution-title"
                or re.fullmatch(
                    r"\s*(?:例(?:题)?|习题|练习|Example|Exercise)\s*[\d一二三四五六七八九十.]+\s*",
                    label,
                    re.I,
                )
            ):
                return
        emit = (
            node.tag in BLOCK_TAGS
            or node.tag == "figure"
            or (own is not None and not has_blocks(node))
        )
        if has_semantic_boundary(node):
            # 段落/行内容器内也可能包含solution标签，先按子树边界拆分再渲染。
            emit = False
        if emit:
            maths, images = [], []
            text = render(node, maths, images, original_uri, original_path)
            text = re.sub(r"[ \t\r\f\v]+", " ", text).strip()
            if text:
                only_images = bool(
                    images and not maths and not re.sub(r"\[图像\]|[\s|]", "", text)
                )
                unit = {
                    "text": text,
                    "kind": (
                        "heading"
                        if is_heading and not group
                        else (
                            "figure" if node.tag == "figure" or only_images else active
                        )
                    ),
                    "heading_level": int(node.tag[1]) if is_heading else None,
                    "source": source_location(),
                    "group": group,
                    "number": number,
                    "reference": reference,
                    "maths": maths,
                    "images": images,
                    "explicit": own is not None,
                    "solution_context": active == "solution",
                }
                append_unit(unit)
            return
        # 非段落容器中的直接文字也要保留，避免只处理 p 时静默丢失题干。
        pending = []

        def flush():
            if not pending:
                return
            temp = Node(
                node.tag,
                node.attrs,
                list(pending),
                node.line,
                node.column,
                node.ordinal,
            )
            maths, images = [], []
            text = re.sub(
                r"\s+", " ", render(temp, maths, images, original_uri, original_path)
            ).strip()
            if text:
                only_images = bool(
                    images and not maths and not re.sub(r"\[图像\]|[\s|]", "", text)
                )
                append_unit(
                    {
                        "text": text,
                        "kind": "figure" if only_images else active,
                        "source": source_location(),
                        "group": group,
                        "number": number,
                        "reference": reference,
                        "maths": maths,
                        "images": images,
                        "explicit": own is not None,
                        "solution_context": active == "solution",
                    }
                )
            pending.clear()

        for child in node.children:
            if isinstance(child, str):
                pending.append(child)
            elif child.tag in SKIP:
                continue
            elif (
                child.tag not in BLOCK_TAGS
                and child.tag != "figure"
                and semantic_kind(child) is None
                and not has_blocks(child)
                and not has_semantic_boundary(child)
            ):
                pending.append(child)
            else:
                flush()
                walk(
                    child, active, group, number, reference, original_uri, original_path
                )
        flush()

    walk(parser.root)
    return units, issues
