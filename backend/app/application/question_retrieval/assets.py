"""从题库的公开字段绑定图像；教材整页不能自动成为公开题图。"""

from app.domains.courses.questions.search.models import QuestionAsset
from app.domains.courses.questions.search.representation import math_assets, public_snapshot, digest
from app.domains.courses.questions.search.store import asset_dict
from app.domains.courses.questions.images import PUBLIC_ROLES, require_public_asset


def bind_assets(store, reader, question):
    snapshot = public_snapshot(question)
    assets = math_assets(snapshot, reader)
    # 教材图示仅接受已做题面裁剪复核的显式绑定；页面原件和解答裁剪不参与此处输入。
    for row in store.assets(question.id):
        if row.role not in PUBLIC_ROLES or row.source.get("binding") != "reviewed_textbook_crop":
            continue
        if row.source.get("section_id") != snapshot.get("textbook_section_id"):
            continue
        value = asset_dict(row)
        try:
            reader.read(value["location"], value["content_hash"])
        except (ValueError, OSError):
            value["status"] = "changed"
        assets.append(value)
    for asset in assets:
        store.insert(QuestionAsset, asset)
    return sorted(assets, key=lambda a: a["id"])


def register_textbook_crop(store, reader, question, path, source, reviewer):
    from app.domains.materials.extraction.models import ExtractedSection
    from pathlib import Path
    if not reviewer or not source.get("no_answer_verified") or not source.get("figure_id"):
        raise ValueError("教材裁剪必须逐图确认题面与答案边界")
    section_id = (question.validation or {}).get("textbook_section_id")
    with store.database() as db:
        section = db.get(ExtractedSection, section_id) if section_id else None
        if section is None or section.status != "reviewed":
            raise ValueError("教材小节尚未通过原页复核")
        figure = next((f for f in section.payload.get("figures", []) if f["id"] == source["figure_id"]), None)
        exercise = next((e for e in section.payload["exercises"] if question.source_ref.endswith("/" + e["source_id"])), None)
        if not figure or not exercise or figure["id"] not in exercise.get("figure_ids", []) or Path(path).resolve() != Path(figure["asset_path"]).resolve():
            raise ValueError("图示与习题来源不一致")
        content = reader.read(path)
        identifier = digest([question.id, str(Path(path).resolve()), content["hash"]])
        asset = {"id": identifier, "question_id": question.id, "course_id": question.course_id,
            "role": "stem", "content_hash": content["hash"], "location": str(Path(path).resolve()), "status": "ready",
            "source": {**source, "section_id": section.id, "binding": "reviewed_textbook_crop", "reviewer": reviewer}}
    store.insert(QuestionAsset, asset)
    return identifier


def public_asset_pixels(asset_id, question_id, course_id, *, store=None, reader=None):
    """讲题与检索复用受控资产，不能通过构造URL读取其他题目的图片。"""
    from app.domains.courses.questions.search.store import QuestionSearchStore
    from app.infrastructure.documents.question_images import QuestionImageReader
    store, reader = store or QuestionSearchStore(), reader or QuestionImageReader()
    row = store.asset(asset_id)
    if not row:
        raise ValueError("配图不属于当前公开题面")
    require_public_asset(asset_dict(row), question_id=question_id, course_id=course_id)
    q = store.question(question_id, course_id)
    assets = bind_assets(store, reader, q)
    if asset_id not in {a["id"] for a in assets if a["status"] == "ready"}:
        raise ValueError("配图已撤回或发生变化")
    return reader.read(row.location, row.content_hash)
