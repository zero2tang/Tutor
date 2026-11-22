"""下载 OpenStax 一元一次方程原始章节，并生成可追溯的教学试点选段。"""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from html.parser import HTMLParser
from pathlib import Path

BOOK_BASE = "https://openstax.org/books/elementary-algebra-2e/pages/"
SECTIONS = {
    "2.3": "2-3-solve-equations-with-variables-and-constants-on-both-sides",
    "2.4": "2-4-use-a-general-strategy-to-solve-linear-equations",
}
EXAMPLES = {
    "2.27",
    "2.29",
    "2.31",
    "2.32",
    "2.35",
    "2.36",
    "2.37",
    "2.38",
    "2.39",
    "2.40",
    "2.41",
    "2.42",
}
VOID_TAGS = {
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
}


@dataclass
class Element:
    tag: str
    attrs: dict
    start: int
    parent: "Element | None"
    end: int = 0
    children: list["Element"] = field(default_factory=list)


class SourceElements(HTMLParser):
    """仅定位源 HTML 区间，选段直接复制原文，避免重新序列化数学标签。"""

    def __init__(self, html: str):
        super().__init__(convert_charrefs=False)
        self.html = html
        self.lines = [0]
        for index, char in enumerate(html):
            if char == "\n":
                self.lines.append(index + 1)
        self.stack: list[Element] = []
        self.elements: list[Element] = []
        self.feed(html)

    def absolute_position(self) -> int:
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def handle_starttag(self, tag, attrs):
        node = Element(
            tag,
            dict(attrs),
            self.absolute_position(),
            self.stack[-1] if self.stack else None,
        )
        self.elements.append(node)
        if node.parent:
            node.parent.children.append(node)
        if tag in VOID_TAGS:
            node.end = self.absolute_position() + len(self.get_starttag_text())
        else:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack and self.stack[-1].tag == tag:
            self.stack.pop().end = self.absolute_position() + len(
                self.get_starttag_text()
            )

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].tag == tag:
                end = self.html.find(">", self.absolute_position()) + 1
                for node in self.stack[index:]:
                    node.end = end
                del self.stack[index:]
                break

    def raw(self, node: Element) -> str:
        return self.html[node.start : node.end]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def curated_excerpt(root: Path, manifest: dict) -> dict:
    blocks, selections = [], []
    for source in manifest["files"]:
        html = (root / source["path"]).read_text(encoding="utf-8")
        parsed = SourceElements(html)
        selected = []
        for node in parsed.elements:
            kind = node.attrs.get("data-type")
            ancestors, parent = [], node.parent
            while parent:
                ancestors.append(parent)
                parent = parent.parent
            # 只保留正文中的概念说明，不混入练习、导航或另一例题内部的段落。
            in_page = any(item.attrs.get("data-type") == "page" for item in ancestors)
            nested = any(
                item.attrs.get("data-type")
                in {"example", "exercise", "note", "solution", "problem"}
                for item in ancestors
            )
            number = None
            if kind == "example":
                numbers = [
                    item
                    for item in parsed.elements
                    if item.attrs.get("class") == "os-number"
                    and item.start > node.start
                    and item.end < node.end
                ]
                if numbers:
                    number = parsed.raw(numbers[0])
                    number = number.split(">", 1)[-1].split("<", 1)[0].strip()
                if number not in EXAMPLES:
                    continue
            elif kind == "note" and "howto" in node.attrs.get("class", "").split():
                pass
            elif node.tag == "p" and in_page and not nested:
                pass
            elif node.tag in {"h2", "h3"} and in_page and not nested:
                pass
            else:
                continue
            if not node.end:
                raise ValueError(
                    f"源元素未闭合：{source['path']} {node.attrs.get('id')}"
                )
            selected.append((node.start, node))
            if kind == "example":
                selections.append(
                    {
                        "number": number,
                        "dom_id": node.attrs.get("id"),
                        "source_url": source["url"],
                        "source_path": source["path"],
                    }
                )
        # 每个选段携带源文件、URL 和未更改的原始 DOM 标识。
        source_blocks = [
            f'<section data-type="source-section" data-source-url="{escape(source["url"], quote=True)}" data-source-path="{source["path"]}">'
        ]
        for _, node in sorted(selected):
            source_blocks.append(parsed.raw(node))
        source_blocks.append("</section>")
        blocks.extend(source_blocks)
    if len(selections) != len(EXAMPLES):
        raise ValueError(
            f"预期 {len(EXAMPLES)} 例，实际 {len(selections)}；官方页面可能已变更。"
        )
    header = '<!doctype html><html lang="en"><head><meta charset="utf-8"><base href="https://openstax.org/"><title>Curated excerpt: OpenStax Elementary Algebra 2e §§2.3–2.4</title></head><body>'
    note = '<aside data-type="curator-notice">Curated excerpt, not an original complete chapter. Selected original explanatory paragraphs, teaching rules and 12 worked examples from OpenStax Elementary Algebra 2e sections 2.3–2.4. Source MathML, solution markup, image descriptions and DOM IDs are preserved. Source image alt text is unverified descriptive evidence, not a mathematical proof. CC BY-NC-SA 4.0; attribution: OpenStax, Rice University. Selection and wrapper by this research prototype.</aside>'
    output = (header + note + "\n" + "\n".join(blocks) + "</body></html>\n").encode(
        "utf-8"
    )
    (root / "linear_equations_curated_excerpt.html").write_bytes(output)
    return {
        "source_kind": "curated_excerpt",
        "path": "linear_equations_curated_excerpt.html",
        "sha256": sha256(output),
        "bytes": len(output),
        "selected_examples": selections,
        "provenance": "Verbatim source DOM fragments with a new wrapper; no model-generated exercise or answer is included.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[2]
        / "data/raw/textbooks/openstax_elementary_algebra_2e",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="只根据已保存原文重建选段，不重新访问网络",
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.offline:
        manifest = json.loads(
            (args.output / "manifest.json").read_text(encoding="utf-8")
        )
        for source in manifest["files"]:
            if sha256((args.output / source["path"]).read_bytes()) != source["sha256"]:
                raise ValueError(f"原始来源校验失败：{source['path']}")
    else:
        files = []
        for section, slug in SECTIONS.items():
            url = BOOK_BASE + slug
            with urllib.request.urlopen(url, timeout=40) as response:
                data = response.read()
                content_type, status = (
                    response.headers.get("Content-Type"),
                    response.status,
                )
            # 以源页面实际许可为准，阻止更新后仍沿用旧许可说明。
            if b"creativecommons.org/licenses/by-nc-sa/4.0" not in data:
                raise ValueError("官方页面许可发生变化，请先核对原文再更新来源清单。")
            filename = section.replace(".", "_") + ".html"
            (args.output / filename).write_bytes(data)
            files.append(
                {
                    "section": section,
                    "url": url,
                    "path": filename,
                    "sha256": sha256(data),
                    "bytes": len(data),
                    "content_type": content_type,
                    "http_status": status,
                    "downloaded_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        manifest = {
            "schema_version": 1,
            "source_kind": "official_textbook_html",
            "book_title": "Elementary Algebra 2e",
            "publisher": "OpenStax, Rice University",
            "language": "en",
            "book_url": "https://openstax.org/details/books/elementary-algebra-2e",
            "chapter_scope": "Chapter 2 selected sections 2.3 and 2.4; linear equations, not a Chinese textbook edition",
            "license": {
                "name": "Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International",
                "spdx": "CC-BY-NC-SA-4.0",
                "url": "https://creativecommons.org/licenses/by-nc-sa/4.0/",
                "observed_on_source": True,
                "usage_note": "Research prototype source. Commercial derivative use requires separate license clearance.",
            },
            "files": files,
        }
    manifest["authors"] = [
        "Lynn Marecek",
        "MaryAnne Anthony-Smith",
        "Andrea Honeycutt Mathis",
    ]
    manifest["publication_date"] = "2020-04-22"
    manifest["bibliographic_metadata_source"] = (
        "citation_* meta elements in the downloaded official pages"
    )
    manifest["curated_excerpt"] = curated_excerpt(args.output, manifest)
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "original_files": len(manifest["files"]),
                "selected_examples": len(
                    manifest["curated_excerpt"]["selected_examples"]
                ),
                "license": manifest["license"]["spdx"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
