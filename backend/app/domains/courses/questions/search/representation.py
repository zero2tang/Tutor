"""可重建的公开题面、资产绑定、来源校验和固定文本模板。"""

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from app.infrastructure.config import ROOT

REPRESENTATION_VERSION = "exercise-grounded-v3"
from app.domains.courses.questions.images import image_bindings
from app.domains.courses.questions.policy import can_search as is_public


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def clean_text(text):
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"<img\b[^>]*>", " ", text, flags=re.I)
    text = re.sub(r"(?:https?://\S+|/api/assets/[^\s)]+)", " ", text)
    return text.strip()


def public_snapshot(q):
    validation = q.validation or {}
    return {"id": q.id, "course_id": q.course_id, "source_id": q.source_id,
            "stem": q.stem, "options": q.options, "version": q.version,
            "question_type": q.question_type, "concept_ids": q.concept_ids,
            "source_ref": q.source_ref, "generated": q.generated,
            "visibility": validation.get("visibility", "public"),
            "availability": validation.get("availability", "ready"),
            "images": validation.get("images", []),
            "canonical_item_id": validation.get("canonical_item_id"),
            "textbook_section_id": validation.get("textbook_section_id")}



def math_assets(snapshot, reader):
    if snapshot["course_id"] != "math":
        return []
    bindings, _ = image_bindings(snapshot)
    assets = []
    for name, fields in bindings.items():
        role = fields[0]
        path = ROOT / "data/raw/xes3g5m/metadata/images" / name
        try:
            image = reader.read(path)
            content_hash, status = image["hash"], "ready"
        except (ValueError, OSError):
            image, content_hash, status = {}, "", "missing"
        identifier = digest([snapshot["id"], name, content_hash])
        assets.append({"id": identifier, "question_id": snapshot["id"], "course_id": snapshot["course_id"],
            "role": role.split(":")[0], "location": str(path), "content_hash": content_hash, "status": status,
            "source": {"url": "/api/assets/math/" + name, "field": role, "fields": fields, "image_identity": image.get("identity")}})
    return assets


def source_hash(snapshot, assets):
    return digest([snapshot, sorted((a["id"], a["content_hash"], a["status"]) for a in assets)])


def validate_extraction(value, assets, snapshot):
    allowed = {a["id"] for a in assets if a["status"] == "ready"}
    if set(value["image_roles"]) - allowed:
        raise ValueError("图片作用引用了未提供的资产")
    native = snapshot["stem"] + "\n" + "\n".join(str(v) for v in snapshot["options"].values())
    for kind in ("ocr", "formulas", "conditions", "goals"):
        for fact in value[kind]:
            evidence = fact["evidence"]
            if evidence["asset_id"]:
                if evidence["asset_id"] not in allowed:
                    raise ValueError("模型引用的图片不属于当前题目")
            elif evidence["evidence_kind"] != "explicit_text" or evidence["quote"] not in native or not evidence["quote"]:
                raise ValueError("文字证据必须能在原题面定位")


def retrieval_text(snapshot, representation=None, *, approved=False):
    parts = [clean_text(snapshot["stem"])]
    parts.extend(f"{k}. {clean_text(str(v))}" for k, v in snapshot["options"].items())
    if approved and representation:
        for kind in ("ocr", "formulas", "conditions", "goals"):
            for fact in representation[kind]:
                if fact["evidence"]["evidence_kind"] != "unknown":
                    parts.append(fact["text"])
    unique, keys = [], []
    for part in parts:
        part = part.strip()
        key = re.sub(r"\s+|\$\$?", "", unicodedata.normalize("NFKC", part))
        if not key or any(key == previous for previous in keys):
            continue
        unique.append(part)
        keys.append(key)
    return "\n".join(unique)


def text_views(text, size=10000):
    # 每个片段单独入库且按同题聚合，不静默截去题末条件。
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def duplicate_key(snapshot, assets):
    explicit = snapshot.get("canonical_item_id")
    if explicit:
        return str(explicit)
    text = unicodedata.normalize("NFKC", retrieval_text(snapshot))
    return digest([re.sub(r"\s+", "", text).casefold(), sorted({a["source"].get("image_identity") or a["content_hash"] for a in assets if a["content_hash"]})])
