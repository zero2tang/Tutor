"""从实际发布边界登记帮助，不把模型自报的零级当成无提示证明。"""

from copy import deepcopy
import re

INDEPENDENT_INTRO = "下面是一道新题。"
INDEPENDENT_INVITATION = "请独立作答，写出你的思路和过程。"
MIXED_INVITATION = "请尝试这道新题，写出你的思路和过程。"

_STRUCTURE_PATTERNS = (
    r"数量关系|建模|模型结构|关系式|列(?:出)?方程|固定(?:费用|部分|量)?",
    r"随.{0,16}(?:变化|改变)|(?:总量|总费用|总花费).{0,30}(?:组成|包含|等于|加)",
    r"(?:部分|费用|距离|路程|时间|速度|数量).{0,20}[+＋=＝].{0,20}(?:总|费用|距离|路程)",
)
_STEM_INSTRUCTION_PATTERNS = (
    r"(?:提示|解答|解析|参考答案|解题思路|解题步骤)\s*[:：]",
    r"由.{1,80}(?:可得|得到|得出|得)\s*[:：]?\s*[$\\(]*\s*[a-zA-Z]\s*=",
    r"(?:因此|所以|故)\s*(?:可得)?\s*[$\\(]*\s*[a-zA-Z]\s*=",
)


def validate_transfer_statement(transfer):
    """拦截显式答案或指导区块；数学语义是否纯题干仍需上游生成与审阅。"""
    if transfer and any(
        re.search(pattern, transfer["stem"]) for pattern in _STEM_INSTRUCTION_PATTERNS
    ):
        raise ValueError(
            "新题stem只能包含题目条件和目标；提示或推导请放teaching_content。"
        )


def _annotation_types(annotations, published_text):
    """只接受实际出现在公开文字中的摘录，防止虚构审计片段。"""
    return {
        item["kind"]
        for item in annotations
        if item.get("excerpt", "").strip() and item["excerpt"].strip() in published_text
    }


def _support(target, content, question, level, action, annotations=()):
    text = "\n\n".join(part for part in (content.strip(), question.strip()) if part)
    if not text:
        return None
    types = _annotation_types(annotations, text)
    if any(re.search(pattern, text) for pattern in _STRUCTURE_PATTERNS):
        types.add("model_structure")
    if action == "worked_example":
        types.add("worked_step")
    # 语义标签可能漏识别，但自由教学文字始终保守计入帮助。
    if not types:
        types.add("unclassified_teaching")
    return {
        "target": target,
        "content": content.strip(),
        "question": question.strip(),
        "help_level": max(
            1,
            level,
            2 if "model_structure" in types else 0,
            3 if action == "worked_example" else 0,
        ),
        "exposure_types": sorted(types),
        "detection_method": "server_publication_boundary",
    }


def prepare_publication(output):
    """规范公开内容并产出内部帮助回执，调用方应在持久化与发送前执行。"""
    result = deepcopy(output)
    exposures = []
    audit = deepcopy(result.get("_publication_audit", {}))
    transfer = result.get("transfer_problem")
    action = result["action"]
    if transfer:
        validate_transfer_statement(transfer)
        independent = transfer.get("publication_mode", "independent") == "independent"
        mixed = action == "worked_example"
        if mixed and independent:
            # 同轮自由示范正文可能夹带新题结构，不能据模型字段保证隔离。
            audit["mixed_independent_request"] = True
            transfer["publication_mode"] = "assisted"
            transfer["help_level"] = max(1, transfer.get("help_level", 0))
            result["question"] = MIXED_INVITATION
            independent = False
        if independent:
            # 零帮助来自受控发布形式，不来自模型声称“没有提示”。
            audit.setdefault(
                "suppressed_independent_fields",
                {
                    "content": result["content"],
                    "question": result.get("question", ""),
                    "focus": result.get("focus", ""),
                    "source_ids": deepcopy(result.get("source_ids", [])),
                    "help_level": result.get("help_level", 0),
                    "support_annotations": deepcopy(
                        result.get("support_annotations", [])
                    ),
                    "teaching_content": transfer.get("teaching_content", ""),
                    "transfer_help_level": transfer.get("help_level", 0),
                    "transfer_support_annotations": deepcopy(
                        transfer.get("support_annotations", [])
                    ),
                },
            )
            transfer["help_level"] = 0
            transfer["teaching_content"] = ""
            transfer["support_annotations"] = []
            result["question"] = INDEPENDENT_INVITATION
            # 关注点和引用标题也会在界面公开，不能绕过中性发布边界。
            result["focus"] = ""
            result["source_ids"] = []
            if action == "transfer":
                result["content"] = INDEPENDENT_INTRO
                result["help_level"] = 0
                result["support_annotations"] = []
        else:
            pieces = []
            if action == "transfer" or mixed:
                pieces.append(result["content"])
            pieces.append(transfer.get("teaching_content", ""))
            new_content = "\n\n".join(
                piece.strip() for piece in pieces if piece.strip()
            )
            new_level = max(
                1,
                transfer.get("help_level", 0),
                result.get("help_level", 0) if action == "transfer" else 0,
            )
            exposure = _support(
                "new",
                new_content,
                result.get("question", ""),
                new_level,
                "transfer",
                [
                    *transfer.get("support_annotations", []),
                    *(
                        result.get("support_annotations", [])
                        if action == "transfer"
                        else []
                    ),
                ],
            )
            if exposure:
                if mixed:
                    exposure["exposure_types"].append("mixed_publication")
                    exposure["detection_method"] = (
                        "server_conservative_mixed_publication"
                    )
                exposures.append(exposure)
                new_level = exposure["help_level"]
            transfer["help_level"] = new_level
            if action == "transfer":
                result["help_level"] = new_level
        if action == "worked_example":
            # 原题示范与新题的发布内容分别归属，不能把旧题示范登记成新题提示。
            exposure = _support(
                "current",
                result["content"],
                "",
                result.get("help_level", 0),
                action,
                result.get("support_annotations", []),
            )
            if exposure:
                exposures.insert(0, exposure)
                result["help_level"] = exposure["help_level"]
    elif not result.get("_server_control_message", False):
        # 模型的结束语仍可能泄露方法；只有服务端自产的控制消息不形成帮助。
        exposure = _support(
            "current",
            result["content"],
            result.get("question", ""),
            result.get("help_level", 0),
            action,
            result.get("support_annotations", []),
        )
        if exposure:
            exposures.append(exposure)
            result["help_level"] = exposure["help_level"]
    result["_publication_support"] = exposures
    result["_publication_audit"] = audit
    return result


def publication_text(output):
    """公共正文统一在这里组装，证据所记片段与实际展示使用同一份输出。"""
    parts = [output["content"]]
    transfer = output.get("transfer_problem")
    if transfer:
        parts.append(transfer["stem"])
        if transfer.get("teaching_content", "").strip():
            parts.append(transfer["teaching_content"])
    question = output.get("question", "").strip()
    if question and question not in "\n\n".join(parts):
        parts.append(question)
    return "\n\n".join(part for part in parts if part.strip())
