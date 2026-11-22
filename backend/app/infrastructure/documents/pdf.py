"""保留整书身份，按页面缓存文字与图像，不用原生文字冒充版式真值。"""

from hashlib import sha256
from pathlib import Path
import json
import re
import unicodedata
import os
import shutil
import subprocess
import pdfplumber


def digest(value):
    raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    return sha256(raw).hexdigest()


def normalized(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def prepare_pdf(source: Path, root: Path, chapter=1):
    raw = source.read_bytes()
    content_hash = digest(raw)
    target = root / content_hash
    target.mkdir(parents=True, exist_ok=True)
    original = target / "original.pdf"
    if not original.exists():
        original.write_bytes(raw)
    elif digest(original.read_bytes()) != content_hash:
        raise ValueError("缓存教材原件哈希不一致，停止提取。")
    metadata = target / f"chapter-{chapter}.json"
    if metadata.exists():
        cached = json.loads(metadata.read_text(encoding="utf-8"))
        if cached.get("discovery") == "native_heading_candidates_v2":
            return cached, target, raw
    chapters = "零一二三四五六七八九十"
    if chapter < 1 or chapter > 9:
        raise ValueError("当前目录发现支持第1—9章。")
    pages = []
    with pdfplumber.open(original) as pdf:
        for number, page in enumerate(pdf.pages, 1):
            cache = target / f"page-{number:03}.json"
            if cache.exists():
                pages.append(json.loads(cache.read_text(encoding="utf-8")))
                continue
            text = page.extract_text() or ""
            lines = [{"text": line["text"], "bbox": [line["x0"], line["top"], line["x1"], line["bottom"]]}
                     for line in page.extract_text_lines()]
            info = {"pdf_page": number, "width": page.width, "height": page.height, "text": text, "lines": lines}
            (target / f"page-{number:03}.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
            pages.append(info)
    def opener(page, index):
        # 目录中同页包含多章标题，不能用它作为正文起点。
        text = normalized(page["text"])
        return f"第{chapters[index]}章" in text[:65] and "目录" not in text[:30] and len(re.findall(r"第[一二三四五六七八九十]+章", text)) == 1
    starts = [p["pdf_page"] for p in pages if opener(p, chapter)]
    if len(starts) != 1:
        raise ValueError(f"第{chapter}章起点不能唯一确定，请复核目录。")
    start = starts[0]
    following = [p["pdf_page"] for p in pages if p["pdf_page"] > start and opener(p, chapter + 1)]
    end = min(following) - 1 if following else len(pages)
    boundaries = [{"number": f"{chapter}:intro", "title": "章节引言", "page_start": start, "kind": "introduction"}]
    for page in pages[start - 1:end]:
        for line in page["lines"]:
            text = normalized(line["text"])
            match = re.fullmatch(rf"({chapter}\.\d+)\s*([\u4e00-\u9fff][\u4e00-\u9fff、与的及和（）()]+)", text)
            if match and line["bbox"][1] < 190:
                boundaries.append({"number": match[1], "title": match[2], "page_start": page["pdf_page"], "kind": "section", "start_y": 0})
            elif text in {"数学活动", "小结", f"复习题{chapter}"} and line["bbox"][1] < 300:
                key = {"数学活动": "activities", "小结": "summary", f"复习题{chapter}": "review"}[text]
                boundaries.append({"number": f"{chapter}:{key}", "title": text, "page_start": page["pdf_page"], "kind": key,
                    "start_y": round(line["bbox"][1] / page["height"] * 1000, 2) if line["bbox"][1] > 130 else 0})
    # 栏目字体没有可用Unicode时，利用目录页码与多个已匹配小节验证偏移，不能按固定封面数猜页码。
    offsets = []
    for boundary in boundaries:
        if boundary["kind"] != "section":
            continue
        for toc in pages[:start - 1]:
            for line in toc["lines"]:
                match = re.fullmatch(re.escape(boundary["number"]) + re.escape(boundary["title"]) + r"(\d{1,3})", normalized(line["text"]))
                if match:
                    offsets.append(boundary["page_start"] - int(match[1]))
    if offsets and len(set(offsets)) == 1:
        offset = offsets[0]
        for toc in pages[:start - 1]:
            for line in toc["lines"]:
                match = re.fullmatch(r"(数学活动|小结|复习题" + str(chapter) + r")(\d{1,3})", normalized(line["text"]))
                if match:
                    page_number = int(match[2]) + offset
                    key = {"数学活动": "activities", "小结": "summary", f"复习题{chapter}": "review"}[match[1]]
                    if start <= page_number <= end and not any(b["number"] == f"{chapter}:{key}" for b in boundaries):
                        boundaries.append({"number": f"{chapter}:{key}", "title": match[1], "page_start": page_number, "kind": key, "start_y": 0})
    unique = {item["number"]: item for item in boundaries}
    sections = sorted(unique.values(), key=lambda x: x["page_start"])
    for index, item in enumerate(sections):
        next_item = sections[index + 1] if index + 1 < len(sections) else None
        item.setdefault("start_y", 0)
        item["page_end"] = next_item["page_start"] - (0 if next_item.get("start_y", 0) > 0 else 1) if next_item else end
        item["end_y"] = next_item.get("start_y", 0) or 1000 if next_item else 1000
        if item["page_end"] < item["page_start"]:
            raise ValueError("小节在同页发生无法自动裁分的边界，请复核。")
    if not any(s["number"] == f"{chapter}.1" for s in sections):
        raise ValueError("未发现可确认的小节标题，停止自动切分。")
    manifest = {"source_hash": content_hash, "page_count": len(pages), "chapter": chapter,
                "chapter_start": start, "chapter_end": end, "sections": sections, "discovery": "native_heading_candidates_v2"}
    metadata.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest, target, raw


def page_input(root: Path, number: int):
    info = json.loads((root / f"page-{number:03}.json").read_text(encoding="utf-8"))
    image = root / f"page-{number:03}-poppler.jpg"
    if not image.exists():
        renderer = os.getenv("TUTOR_PDFTOPPM") or shutil.which("pdftoppm")
        if not renderer:
            raise RuntimeError("缺少Poppler，请配置TUTOR_PDFTOPPM；不能降级为未核对的渲染器。")
        command = [renderer, "-f", str(number), "-l", str(number), "-r", "145", "-singlefile", "-jpeg", "-jpegopt", "quality=92",
                   str(root / "original.pdf"), str(image.with_suffix(""))]
        result = subprocess.run(command, capture_output=True, timeout=90, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode or not image.exists():
            raise RuntimeError("Poppler页面渲染失败，未向模型提交损坏图像。")
    info["image_path"] = str(image)
    info["image_hash"] = digest(image.read_bytes())
    return info


def crop_region(root, page, bbox, name):
    from PIL import Image
    source = page_input(root, page)
    image = Image.open(source["image_path"])
    w, h = image.size
    box = tuple(round(value * (w if index % 2 == 0 else h) / 1000) for index, value in enumerate(bbox))
    output = root / "regions" / f"{name}.jpg"
    output.parent.mkdir(exist_ok=True)
    image.crop(box).save(output, quality=94)
    return str(output)
