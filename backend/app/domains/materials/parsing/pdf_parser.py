"""原生 PDF 的词坐标、分栏和页眉页脚规则；不推测扫描图中的文字。"""

from collections import Counter
from io import BytesIO
import re


def extract_pdf(content: bytes):
    try:
        import pdfplumber
    except ImportError as exc:
        from .errors import UnsupportedDocument

        raise UnsupportedDocument(
            "PDF 解析需要安装 pdfplumber；未调用 OCR 或视觉模型。"
        ) from exc
    units, issues, pages = [], [], []
    try:
        with pdfplumber.open(BytesIO(content)) as pdf:
            if len(pdf.pages) > 400:
                raise ValueError("PDF 超过 400 页，请按章节拆分。")
            for page_number, page in enumerate(pdf.pages, 1):
                words = page.extract_words(
                    x_tolerance=2, y_tolerance=3, extra_attrs=["size", "fontname"]
                )
                pages.append(
                    {
                        "number": page_number,
                        "width": float(page.width),
                        "height": float(page.height),
                        "words": words,
                        "images": list(page.images),
                        "chars": list(page.chars),
                    }
                )
    except Exception as exc:
        from .errors import UnsupportedDocument

        raise UnsupportedDocument(
            "PDF 无法可靠读取，可能已加密、损坏或超出解析范围。"
        ) from exc
    edge_texts = Counter()
    candidates = []
    for page in pages:
        lines = _lines(page["words"])
        edge = set()
        for line in lines:
            if (
                line["top"] < page["height"] * 0.08
                or line["bottom"] > page["height"] * 0.93
            ):
                signature = re.sub(r"\d+", "#", line["text"])
                edge.add(signature)
        edge_texts.update(edge)
        candidates.append(lines)
    for page, lines in zip(pages, candidates):
        page_number = page["number"]
        if not page["words"]:
            issues.append(
                {
                    "code": "image_only_page",
                    "severity": "review",
                    "message": "本页没有可提取的原生文本；扫描内容未识别。",
                    "source": {
                        "page": page_number,
                        "locator": f"pdf:page:{page_number}",
                        "bbox": [0, 0, page["width"], page["height"]],
                    },
                    "fallback": "ocr_then_visual_or_human_review",
                }
            )
        for image_no, image in enumerate(page["images"], 1):
            bbox = [
                float(image.get("x0", 0)),
                float(image.get("top", 0)),
                float(image.get("x1", 0)),
                float(image.get("bottom", 0)),
            ]
            source = {
                "page": page_number,
                "locator": f"pdf:page:{page_number}:image:{image_no}",
                "bbox": bbox,
            }
            units.append(
                {
                    "text": "[PDF 图像区域]",
                    "kind": "figure",
                    "source": source,
                    "association_unknown": True,
                    "maths": [],
                    "images": [
                        {
                            "src": None,
                            "source_description": "",
                            "source": source,
                            "status": "needs_review",
                        }
                    ],
                }
            )
            issues.append(
                {
                    "code": "image_content_unread",
                    "severity": "review",
                    "message": "已定位 PDF 图像，图中条件和文字尚未识别。",
                    "source": source,
                    "fallback": "ocr_then_visual_or_human_review",
                }
            )
        kept = []
        for line in lines:
            signature = re.sub(r"\d+", "#", line["text"])
            at_edge = (
                line["top"] < page["height"] * 0.08
                or line["bottom"] > page["height"] * 0.93
            )
            if (
                at_edge
                and len(pages) >= 2
                and edge_texts[signature] >= max(2, len(pages) // 2)
            ):
                continue
            kept.append(line)
        # 连续留白的中间栏缝至少覆盖三行，且没有文字横跨时，按左栏→右栏恢复顺序。
        center = page["width"] / 2
        crossing = [
            w
            for w in page["words"]
            if float(w["x0"]) < center - 8 and float(w["x1"]) > center + 8
        ]
        split_lines = 0
        for line in kept:
            left = [w for w in line["words"] if float(w["x1"]) < center - 14]
            right = [w for w in line["words"] if float(w["x0"]) > center + 14]
            if (
                left
                and right
                and not any(
                    float(w["x0"]) < center + 14 and float(w["x1"]) > center - 14
                    for w in line["words"]
                )
            ):
                split_lines += 1
        if split_lines >= 3 and not crossing:
            # 重新按栏分组，避免同一水平线上左右栏内容拼接。
            left_words = [
                w for line in kept for w in line["words"] if float(w["x1"]) <= center
            ]
            right_words = [
                w for line in kept for w in line["words"] if float(w["x0"]) > center
            ]
            kept = _lines(left_words) + _lines(right_words)
        for line_no, line in enumerate(kept, 1):
            source = {
                "page": page_number,
                "locator": f"pdf:page:{page_number}:line:{line_no}",
                "bbox": [line["x0"], line["top"], line["x1"], line["bottom"]],
            }
            text = line["text"]
            sizes = [float(w.get("size", 0)) for w in line["words"]]
            kind = (
                "heading"
                if re.match(r"^第[一二三四五六七八九十\d]+[章节]", text)
                else None
            )
            # 不从字号差猜出指数或分式。只要一行的字形尺度混杂且含等号，就进入公式复核。
            uncertain = bool("=" in text and sizes and min(sizes) < max(sizes) * 0.8)
            if "=" in text:
                height = max(line["bottom"] - line["top"], 1)
                nearby = [
                    other
                    for other in kept
                    if other is not line
                    and 3 < abs(other["top"] - line["top"]) < height * 1.4
                    and other["x0"] < line["x1"] + height
                    and other["x1"] > line["x0"] - height
                ]
                # 分式和上下标可能被分成相邻短行；不能把其中一行当完整等式。
                uncertain = uncertain or any(
                    re.fullmatch(r"[\daxym+\-*/(). ]+", other["text"])
                    for other in nearby
                )
            if uncertain:
                issues.append(
                    {
                        "code": "pdf_formula_layout_uncertain",
                        "severity": "review",
                        "message": "本行含不同字号的公式元素，未将二维结构压平成可信公式。",
                        "source": source,
                        "fallback": "formula_review",
                    }
                )
            units.append(
                {
                    "text": text,
                    "kind": kind,
                    "heading_level": 2 if kind else None,
                    "source": source,
                    "maths": [],
                    "images": [],
                    "layout_uncertain": uncertain,
                    "native_words": [
                        {
                            "text": w["text"],
                            "bbox": [
                                float(w["x0"]),
                                float(w["top"]),
                                float(w["x1"]),
                                float(w["bottom"]),
                            ],
                            "size": float(w.get("size", 0)),
                        }
                        for w in line["words"]
                    ],
                }
            )
    return units, issues


def _lines(words):
    grouped = []
    for word in sorted(
        words, key=lambda w: (round(float(w["top"]) / 3) * 3, float(w["x0"]))
    ):
        target = next(
            (
                line
                for line in grouped[-3:]
                if abs(line["top"] - float(word["top"])) <= 3
            ),
            None,
        )
        if target is None:
            target = {"top": float(word["top"]), "words": []}
            grouped.append(target)
        target["words"].append(word)
    result = []
    for line in grouped:
        items = sorted(line["words"], key=lambda w: float(w["x0"]))
        text = " ".join(w["text"] for w in items).strip()
        if text:
            result.append(
                {
                    "text": text,
                    "words": items,
                    "x0": min(float(w["x0"]) for w in items),
                    "x1": max(float(w["x1"]) for w in items),
                    "top": min(float(w["top"]) for w in items),
                    "bottom": max(float(w["bottom"]) for w in items),
                }
            )
    return sorted(result, key=lambda x: (x["top"], x["x0"]))
