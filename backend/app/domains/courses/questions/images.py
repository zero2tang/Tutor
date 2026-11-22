"""公开题图的链接与字段归属；不读取答案、解析或任意网络地址。"""

import re

MATH_IMAGE_URL = re.compile(r"(?<![\w:/%.])/api/assets/math/(question_\d+-image_\d+\.png)(?![\w./%])")
ASSET_IMAGE_URL = re.compile(r"(?<![\w:/%.])/api/assets/exercises/([a-f0-9]{64})(?![\w./%])")
PRIVATE_FIELDS = {"answer", "answer_key", "explanation", "analysis", "analysis_images", "validation"}
PUBLIC_ROLES = {"stem", "option"}


def require_public_asset(asset, *, question_id=None, course_id=None):
    if asset["role"] not in PUBLIC_ROLES or asset["status"] != "ready":
        raise ValueError("配图不是当前可读的公开题面资产")
    if (question_id and asset["question_id"] != question_id) or (course_id and asset["course_id"] != course_id):
        raise ValueError("配图不属于当前题目或课程")
    if str(asset["location"]).replace("\\", "/").rsplit("/", 1)[-1].startswith("analysis_"):
        raise ValueError("解析图片不能作为公开题面输入")


def public_math_image(filename, alt="题目配图"):
    if not isinstance(filename, str) or filename.startswith("analysis_") or not re.fullmatch(r"[A-Za-z0-9_.-]+\.(?:png|jpg|jpeg|webp|gif)", filename, re.I):
        return None
    return {"url": "/api/assets/math/" + filename, "alt": str(alt)[:200]}


def public_images(question, assets=None):
    """目录投影仅输出链接；通过校验的教材资产可补充公开题图。"""
    value = question if isinstance(question, dict) else None
    validation = (value.get("validation", {}) if value else getattr(question, "validation", {})) or {}
    course_id = value.get("course_id") if value else question.course_id
    images = []
    if course_id == "math":
        for item in validation.get("images", []):
            filename = item if isinstance(item, str) else item.get("filename", "") if isinstance(item, dict) else ""
            image = public_math_image(filename, item.get("alt", "题目配图") if isinstance(item, dict) else "题目配图")
            if image and image not in images:
                images.append(image)
    for asset in assets or []:
        if asset["status"] == "ready" and asset["role"] in PUBLIC_ROLES and not asset["source"].get("url"):
            images.append({"url": "/api/assets/exercises/" + asset["id"], "alt": "已复核教材题图"})
    return images


def image_bindings(question):
    """题干、选项及公开images共用解析，保留选项字母和同图多处引用。"""
    options = question.get("options") or {}
    values = [("stem", question.get("stem", "")), *[("option:" + str(k), v) for k, v in options.items() if isinstance(v, str)]] if isinstance(options, dict) else [("stem", question.get("stem", ""))]
    math, assets = {}, {}
    def add(field, text):
        if not isinstance(text, str):
            return
        for pattern, output in ((MATH_IMAGE_URL, math), (ASSET_IMAGE_URL, assets)):
            for match in pattern.finditer(text):
                fields = output.setdefault(match.group(1), [])
                if field not in fields:
                    fields.append(field)
    for field, value in values:
        add(field, value)
    explicit = set(math)
    for item in question.get("images") or []:
        filename = item if isinstance(item, str) else item.get("filename", "") if isinstance(item, dict) else ""
        if re.fullmatch(r"question_\d+-image_\d+\.png", filename) and filename not in explicit:
            add("stem", "/api/assets/math/" + filename)
        if isinstance(item, dict):
            add("stem", item.get("url", ""))
    return math if question.get("course_id") == "math" else {}, assets
