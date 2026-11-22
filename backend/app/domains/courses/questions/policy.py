"""题目能力规则：可公开读取、可共享检索、可独立练习与解析授权各自判断。"""

from dataclasses import dataclass
import re
from sqlalchemy import func
from .images import public_images

STATUSES = {"ready", "missing_figure", "ambiguous_options", "insufficient_options", "duplicate_options", "invalid_answer", "reference_only", "source_incomplete"}
NOTES = {
    "missing_figure": "原始数据提到配图，但未提供对应图像，暂不用于独立评估。",
    "ambiguous_options": "原始选项存在重复或歧义，暂不用于独立评估。",
    "insufficient_options": "原始题目选项不足，暂不用于独立评估。",
    "duplicate_options": "原始题目包含重复干扰项，暂不用于独立评估。",
    "invalid_answer": "原始答案无法对应有效选项，暂不用于独立评估。",
    "reference_only": "该题仅作参考资料，尚未标定为可独立评分题。",
    "source_incomplete": "题目已撤回或来源尚未确认，暂不提供公开检索与独立练习。",
}


def field(question, name, default=None):
    return question.get(name, default) if isinstance(question, dict) else getattr(question, name, default)


@dataclass(frozen=True)
class QuestionAccess:
    availability: str
    note: str
    public_readable: bool
    searchable: bool
    independent_practice: bool
    solution_readable: bool


def access(question, *, assets=None, solution_authorized=False, allow_archived_solution=False):
    validation = field(question, "validation", {}) or {}
    raw = validation.get("availability", field(question, "availability", "ready"))
    raw = "ready" if raw is None else raw
    status = raw if isinstance(raw, str) and raw in STATUSES else "source_incomplete"
    if validation.get("reference_only") and status == "ready":
        status = "reference_only"
    images = public_images(question, assets) if not isinstance(question, dict) or "validation" in question else question.get("images", [])
    figure_available = validation.get("figure_available", bool(images))
    if assets:
        figure_available = all(a["status"] == "ready" for a in assets) and bool(images)
    missing = bool(validation.get("missing_images")) or (bool(re.search(r"如图|下图|图示|见图", field(question, "stem", ""))) and not figure_available)
    # 撤回状态不能被“缺图”覆盖后重新变成可检索题。
    if missing and status != "source_incomplete":
        status = "missing_figure"
    visibility = validation.get("visibility", field(question, "visibility", "public"))
    visible = ("public" if visibility is None else visibility) == "public"
    public = visible and status != "source_incomplete"
    note = str(validation.get("availability_note", field(question, "availability_note", "")))[:600] or NOTES.get(status, "")
    return QuestionAccess(status, note, public, public and not field(question, "generated", False),
        public and status == "ready", bool(solution_authorized and (public or allow_archived_solution)))


def can_search(question):
    return access(question).searchable


def can_practice(question):
    return access(question).independent_practice


def require_access(question, purpose="read", *, solution_authorized=False, allow_archived_solution=False):
    rights = access(question, solution_authorized=solution_authorized, allow_archived_solution=allow_archived_solution)
    permitted = {"read": rights.public_readable, "search": rights.searchable,
        "practice": rights.independent_practice, "solution": rights.solution_readable}
    if purpose not in permitted or not permitted[purpose]:
        raise ValueError(rights.note or "当前题目或解析不在本次操作的可读范围内")
    return rights


def searchable_conditions(model):
    """SQL分页与Python规则采用相同公开状态和生成题边界。"""
    return [model.generated == False,
        func.coalesce(model.validation["visibility"].as_string(), "public") == "public",
        func.coalesce(model.validation["availability"].as_string(), "ready").in_(sorted(STATUSES - {"source_incomplete"}))]
