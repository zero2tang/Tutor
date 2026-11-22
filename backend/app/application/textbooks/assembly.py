"""给模型转录补上程序生成的来源和资产，跨页题目只按显式续接合并。"""

from copy import deepcopy
import re
import unicodedata
from app.infrastructure.documents.pdf import crop_region, digest
from app.domains.materials.extraction.schemas import Region


def attach_page(page, value, root):
    number = page["pdf_page"]
    def source(bbox):
        Region(bbox=bbox)
        return {"pdf_page": number, "printed_page": value["printed_page"], "bbox_normalized": bbox,
                "bbox_origin": "model_estimated_region",
                "bbox": [round(v * (page["width"] if i % 2 == 0 else page["height"]) / 1000, 3) for i, v in enumerate(bbox)],
                "page_image": page["image_path"], "page_image_hash": page["image_hash"]}
    result = deepcopy(value)
    figures = {item["id"] for item in result["figures"]}
    if len(figures) != len(result["figures"]):
        raise ValueError("当前页图示编号重复，拒绝错误关联。")
    for kind, prefix in (("definitions", "d"), ("teaching_blocks", "t"), ("exercises", "e"), ("methods", "m"), ("figures", "f")):
        for index, item in enumerate(result[kind], 1):
            item["source_id"] = f"p{number:03}{prefix}{index:02}"
            item["source_refs"] = [source(item["bbox"])]
            if set(item.get("figure_ids", [])) - figures:
                raise ValueError("题目或正文引用了当前页不存在的图片。")
            if kind == "figures":
                item["asset_path"] = crop_region(root, number, item["bbox"], item["source_id"])
                item["role_origin"] = "model_inferred_from_page"
                item["description_origin"] = "model_image_summary"
                item["description_notice"] = "图中数学条件以原图为准；模型描述和估计坐标不是逐字人工标注。"
            elif kind in {"definitions", "exercises"}:
                item["origin"] = "textbook_transcription"
            elif kind == "teaching_blocks":
                item["text_origin"], item["role_origin"] = "textbook_transcription", "model_inferred_from_page"
            if kind == "exercises" and item.get("solution_bbox") is not None:
                item["solution_refs"] = [source(item["solution_bbox"])]
            item["figure_ids"] = [f"p{number:03}:{key}" for key in item.get("figure_ids", [])]
            if kind == "figures":
                item["id"] = f"p{number:03}:{item['id']}"
    result["pdf_page"] = number
    result["exercises"].sort(key=lambda item: (item["bbox"][1], item["bbox"][0]))
    return result


def assemble(section, pages):
    result = {**section, "schema_version": "textbook-three-resources-v1", "pages": pages,
              "definitions": [], "exercises": [], "methods": [], "figures": [], "issues": []}
    for page in pages:
        for kind in ("definitions", "methods", "figures"):
            result[kind].extend(deepcopy(page[kind]))
        result["issues"].extend({"pdf_page": page["pdf_page"], "message": message} for message in page["issues"])
        for item in deepcopy(page["exercises"]):
            if item["continued_from_previous"]:
                previous = result["exercises"][-1] if result["exercises"] else None
                def canonical(value):
                    match = re.search(r"\d+(?:\.\d+)*", unicodedata.normalize("NFKC", value))
                    return match.group(0) if match else value
                if previous is None or previous["kind"] != item["kind"] or (item["number"] and canonical(previous["number"]) != canonical(item["number"])):
                    raise ValueError("跨页题目不能唯一关联，需复核后重跑，不能绑定其他题。")
                previous["stem"] += "\n" + item["stem"]
                for key in ("subquestions", "tables", "figure_ids", "source_refs"):
                    previous[key].extend(item.get(key, []))
                for key in ("answer", "solution"):
                    if item.get(key):
                        previous[key] = "\n".join(filter(None, [previous.get(key), item[key]]))
                previous.setdefault("solution_refs", []).extend(item.get("solution_refs", []))
            else:
                result["exercises"].append(item)
    result["coverage"] = {"pdf_pages": [p["pdf_page"] for p in pages],
        "definitions": len(result["definitions"]), "exercises": len(result["exercises"]),
        "methods": len(result["methods"]), "figures": len(result["figures"]), "issues": len(result["issues"])}
    return result


def design_input(payload):
    sources = []
    for page in payload["pages"]:
        for kind in ("definitions", "teaching_blocks", "methods", "figures"):
            for item in page[kind]:
                sources.append({"source_id": item["source_id"], "page": page["pdf_page"],
                                "kind": kind, "role": item.get("role"),
                                "text": item.get("text") or item.get("statement") or item.get("description"),
                                "bbox": item["bbox"], "name": item.get("name")})
        # 教学模式只需要题目用途与起始内容；完整题干与答案仍保存在习题记录中。
        for item in page["exercises"]:
            sources.append({"source_id": item["source_id"], "page": page["pdf_page"], "kind": item["kind"],
                            "group": item["group"], "text": item["stem"][:240], "bbox": item["bbox"]})
    sources.sort(key=lambda item: (item["page"], item["bbox"][1], item["bbox"][0]))
    return {"section": payload["number"], "title": payload["title"], "sources": sources}


def validate_design(design, payload):
    available = {item["source_id"]: item["source_refs"] for p in payload["pages"]
                 for kind in ("definitions", "teaching_blocks", "methods", "exercises", "figures") for item in p.get(kind, [])}
    for kind in ("steps", "key_questions", "potential_difficulties", "methods"):
        for item in design[kind]:
            if set(item["source_ids"]) - set(available):
                raise ValueError("教学归纳包含不存在的来源。")
            item["source_refs"] = [ref for key in item["source_ids"] for ref in available[key]]
    design["origin"] = "model_summarized_from_source"
    design["notice"] = "教学用途与可能困难是模型归纳，不代表编者逐字声明、实际学生误解或教师审校。"
    return design
