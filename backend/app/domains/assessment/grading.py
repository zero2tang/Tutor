"""客观判分先于模型诊断；未知答案交由受约束评估处理。"""

import json
import re
import unicodedata


def normalize(text: str) -> str:
    """统一全半角、大小写和常见格式符号，用于字面匹配，不证明数学表达式等价。"""
    value = unicodedata.normalize("NFKC", str(text)).casefold()
    # 花括号、反斜线和标点可能携带数学结构，不能把不同分数或数列合并。
    return re.sub(r"\s+", " ", value).strip().strip("$").strip()


def deterministic_grade(contract: dict, answer_text: str) -> dict | None:
    """用题库答案执行确定性判分，无法可靠匹配时返回 None 交给模型诊断。

    选择题比较标签集合，多选漏选可得部分分；多空题按空位键匹配，
    单空允许多个标准写法。填空未匹配不直接判错，以免误判等价表达式。
    """
    expected = contract["answer"]
    kind = contract["question_type"]
    if kind in {"choice", "multi_choice"}:
        text = answer_text.strip().upper()
        labels = re.findall(r"[A-Z]", text)
        allowed = set(contract["options"])
        # 纯标签或带选项前缀的回答允许直接判分；长段解释避免误提取英文符号。
        if (
            not labels
            or any(label not in allowed for label in labels)
            or len(text) > 25
        ):
            matched = [
                key
                for key, value in contract["options"].items()
                if normalize(value) == normalize(answer_text)
            ]
            if len(matched) != 1:
                return None
            labels = matched
        chosen = set(labels)
        if kind == "choice" and len(chosen) != 1:
            return {
                "result": "ungraded",
                "score": None,
                "feedback": "请只选择一个选项。",
                "confidence": 1.0,
            }
        correct = set(str(x).upper() for x in expected)
        if chosen == correct:
            return {
                "result": "correct",
                "score": 1.0,
                "feedback": "选项与题库标准答案一致。",
                "confidence": 1.0,
            }
        if kind == "multi_choice" and chosen and chosen < correct:
            return {
                "result": "partial",
                "score": len(chosen) / len(correct),
                "feedback": "部分选项正确，但存在漏选。",
                "confidence": 1.0,
            }
        return {
            "result": "incorrect",
            "score": 0.0,
            "feedback": "选项与题库标准答案不一致。",
            "confidence": 1.0,
        }
    if isinstance(expected, dict):
        try:
            values = json.loads(answer_text)
        except json.JSONDecodeError:
            parts = re.split(r"[；;，,\n]+", answer_text)
            values = {str(index + 1): value for index, value in enumerate(parts)}
        if isinstance(values, dict) and set(values) == set(expected):
            matches = [
                normalize(values[key]) in {normalize(item) for item in acceptable}
                for key, acceptable in expected.items()
            ]
            if all(matches):
                return {
                    "result": "correct",
                    "score": 1.0,
                    "feedback": "各填空与题库答案一致。",
                    "confidence": 1.0,
                }
        return None
    values = expected if isinstance(expected, list) else [expected]
    if normalize(answer_text) in {normalize(v) for v in values}:
        return {
            "result": "correct",
            "score": 1.0,
            "feedback": "与题库答案一致。",
            "confidence": 1.0,
        }
    return None
