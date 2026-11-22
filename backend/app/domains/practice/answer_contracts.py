"""复核答案的可执行协议；仅映射选项身份，不解释或求解数学表达式。"""

import re

from app.domains.assessment.grading import normalize


def choice_answer_labels(options, answer):
    """把完整标签或唯一匹配的选项原文映射为集合，保留完整多选答案边界。"""
    labels = []
    for value in answer:
        text = normalize(value)
        # 只接受整段都是标签的写法，不能从说明文字中抓取零散字母。
        if re.fullmatch(r"[a-d](?:[\s,，、;；/]*[a-d])*", text):
            labels.extend(re.findall(r"[A-D]", text.upper()))
            continue
        matches = [
            label for label, option in options.items() if normalize(option) == text
        ]
        if len(matches) != 1:
            return None
        labels.append(matches[0])
    if (
        not labels
        or len(labels) != len(set(labels))
        or not set(labels).issubset(options)
    ):
        return None
    return tuple(sorted(labels))


def review_contract_issue(draft, independent, review):
    """程序只守字段回执、显式裁定与选项集合；填空数学含义仍由模型复核。"""
    for name, source in (("generation", draft), ("independent", independent)):
        assessment = review[f"{name}_assessment"]
        if assessment["submitted_answer"] != source["answer"]:
            return f"{name}_assessment.submitted_answer必须原样回执对应输入answer，不能用解析中的答案替换。"
    if review["verdict"] not in {"equivalent", "complementary"}:
        return ""
    if any(
        not source["answer"] or any(not value.strip() for value in source["answer"])
        for source in (draft, independent)
    ):
        return "两份输入都必须有实际非空答案才能接受，不能把缺答当作正确的部分结论。"
    for name in ("generation", "independent"):
        assessment = review[f"{name}_assessment"]
        if (
            assessment["correctness"] != "correct"
            or assessment["answer_explanation_consistency"] != "consistent"
        ):
            return "输入答案错误、与自身解析矛盾或无法确认时不能接受，必须返回非接受裁定和空answer。"
    if draft["question_type"] in {"choice", "multi_choice"}:
        answers = [
            choice_answer_labels(draft["options"], output["answer"])
            for output in (draft, independent, review)
        ]
        if None in answers or not (answers[0] == answers[1] == answers[2]):
            return "完整选择题答案集合不一致或无法可靠映射，不能用解析修正后称equivalent/complementary；请返回repair_required或conflict并令answer=[]。"
    return ""
