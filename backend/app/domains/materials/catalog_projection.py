"""把教材原文投影为可追溯参考目录，不求解、不判题、不生成教学结论。"""

from copy import deepcopy
from hashlib import sha256
import json
import re

VERSION = "textbook-catalog-v1"


def catalog_hash(value: dict) -> str:
    return sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _refs(blocks: dict, identifiers: list[str]) -> list[dict]:
    return [
        {**deepcopy(blocks[identifier].get("source", {})), "block_id": identifier}
        for identifier in dict.fromkeys(identifiers)
        if identifier in blocks
    ]


def _answer(exercise, blocks):
    # 只复制显式答案，或解析“答案：...”原文标签，不从步骤计算答案。
    if exercise.get("answer") is not None:
        return deepcopy(exercise["answer"])
    for identifier in exercise.get("solution_block_ids", []):
        block = blocks.get(identifier, {})
        match = re.fullmatch(
            r"\s*(?:答案|答|Answer)\s*[：:]\s*(.+)\s*",
            block.get("text", ""),
            re.I | re.S,
        )
        if match:
            return match[1].strip()
    return None


def _project(parsed: dict, exercise: dict) -> dict:
    document = parsed.get("document", {})
    blocks = {block["id"]: block for block in parsed.get("blocks", [])}
    source_ids = list(exercise.get("source_block_ids", []))
    solution_ids = list(exercise.get("solution_block_ids", []))
    identifiers = set(source_ids + solution_ids)
    issues = []
    if not source_ids:
        issues.append(
            {
                "code": "missing_problem_source",
                "severity": "blocking",
                "message": "题目缺少可定位的原文块。",
            }
        )
    for identifier in identifiers:
        if identifier not in blocks:
            issues.append(
                {
                    "code": "unresolved_source",
                    "severity": "blocking",
                    "block_id": identifier,
                    "message": "来源块不存在，保留草稿等待修复。",
                }
            )
    locators = {
        blocks[identifier].get("source", {}).get("locator")
        for identifier in identifiers
        if identifier in blocks
    }
    for issue in parsed.get("issues", []):
        if (
            issue.get("exercise_id") == exercise["id"]
            or issue.get("block_id") in identifiers
            or issue.get("source", {}).get("locator") in locators
        ):
            issues.append(deepcopy(issue))
    for identifier in identifiers:
        block = blocks.get(identifier)
        if block and (
            block.get("needs_review")
            or block.get("association_status") == "needs_review"
        ):
            issues.append(
                {
                    "code": "source_layout_review",
                    "severity": "review",
                    "block_id": identifier,
                    "message": "原文版式或关联仍需复核，未尝试推断数学含义。",
                }
            )
        if block and block.get("images"):
            issues.append(
                {
                    "code": "source_figure_unread",
                    "severity": "review",
                    "block_id": identifier,
                    "message": "已保留原始图示及描述；图中数学条件尚未转录。",
                }
            )
    answer = _answer(exercise, blocks)
    has_answer = answer is not None and answer != "" and answer != []
    blocking = any(issue.get("severity") in {"blocking", "error"} for issue in issues)
    review = any(issue.get("severity") == "review" for issue in issues)
    if not has_answer:
        issues.append(
            {
                "code": "source_answer_missing",
                "severity": "info",
                "message": "原文没有显式答案，保留题干和解答内容供引导教学参考。",
            }
        )
    source_solution = [
        {
            "block_id": identifier,
            "text": blocks[identifier].get("text", ""),
            "equations": deepcopy(blocks[identifier].get("equations", [])),
            "source_refs": _refs(blocks, [identifier]),
            "provenance": {"origin": "textbook_transcription"},
        }
        for identifier in solution_ids
        if identifier in blocks
    ]
    pack = {
        "id": "teaching:" + exercise["id"],
        "question_id": exercise["id"],
        "document_id": document.get("id"),
        "chapter_id": exercise.get("chapter_id"),
        "version": VERSION,
        "status": (
            "draft"
            if blocking
            else "reference_only" if review or not has_answer else "ready"
        ),
        "problem": {
            "stem": exercise.get("stem", ""),
            "equation": exercise.get("equation"),
            "number": exercise.get("number"),
            "kind": exercise.get("kind"),
        },
        "source_refs": _refs(blocks, source_ids + solution_ids),
        "source_answer": {
            "value": answer,
            "source_refs": _refs(blocks, solution_ids),
            "provenance": {"origin": "textbook_transcription"},
        },
        "source_solution": source_solution,
        "source_solution_steps": deepcopy(exercise.get("solution_steps", [])),
        "solution_paths": [],
        "concepts": [],
        "techniques": [],
        "related_examples": [],
        "diagnostic_probes": [],
        "hint_ladder": [],
        "issues": issues,
        "verification": {
            "result": "not_checked",
            "method": "source_structure_integrity",
            "scope": "仅校验来源、结构与指纹；未验证数学正确性、原文答案或学生掌握程度。",
        },
        "provenance": {
            "document_content_hash": document.get("content_hash"),
            "parser_version": document.get("parser_version"),
            "source_kind": document.get("source_kind"),
            "edition": document.get("edition"),
            "analysis_version": VERSION,
            "origin": "source_projection",
            "teacher_reviewed": False,
            "mathematics_checked": False,
        },
    }
    pack["hash"] = catalog_hash(pack)
    return pack


def project_catalog(parsed: dict) -> list[dict]:
    """输入一个结构化文档，任意题型都保留为来源目录，不产生参考解。"""
    return [_project(parsed, exercise) for exercise in parsed.get("exercises", [])]


def verify_catalog_pack(pack: dict, parsed: dict | None = None) -> None:
    """读取时校验不可变来源；历史分析档案也仅核对完整性，不重新求解。"""
    if pack.get("hash") != catalog_hash(
        {key: value for key, value in pack.items() if key != "hash"}
    ):
        raise ValueError("教学资料内容哈希不一致。")
    if parsed is None:
        return
    document = parsed.get("document", {})
    if pack.get("document_id") != document.get("id") or pack.get("provenance", {}).get(
        "document_content_hash"
    ) != document.get("content_hash"):
        raise ValueError("教学资料与教材来源或版本不一致。")
    existing = {block["id"] for block in parsed.get("blocks", [])}
    unresolved = set()

    def visit(value):
        if isinstance(value, dict):
            for key, part in value.items():
                if key == "source_refs":
                    for ref in part:
                        if ref.get("block_id") not in existing:
                            unresolved.add(ref.get("block_id"))
                        if ref.get("document_id") not in {None, document.get("id")}:
                            raise ValueError("教学资料包含跨文档原文引用。")
                else:
                    visit(part)
        elif isinstance(value, list):
            for part in value:
                visit(part)

    visit(pack)
    if unresolved:
        raise ValueError("教学资料包含无法解析的原文引用。")
