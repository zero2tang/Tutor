"""只恢复可唯一定位的排版差异，不把模型改写当成来源证据。"""

import re
from app.domains.courses.questions.search.representation import validate_extraction


def restore_literal_quotes(value, assets, snapshot):
    sources = [snapshot["stem"], *map(str, snapshot["options"].values())]
    repairs = []
    for kind in ("ocr", "formulas", "conditions", "goals"):
        for fact in value[kind]:
            evidence = fact["evidence"]
            quote = evidence["quote"]
            if evidence["asset_id"] or any(quote in source for source in sources):
                continue
            # 只忽略LaTeX美元分隔符与空白，数字、汉字、标点和运算符必须完全一致。
            needle = re.sub(r"[\s$]", "", quote)
            if not needle:
                continue
            matches = []
            for source in sources:
                offsets = [
                    i for i, c in enumerate(source) if c != "$" and not c.isspace()
                ]
                normalized = "".join(source[i] for i in offsets)
                start = normalized.find(needle)
                while start >= 0:
                    left, right = offsets[start], offsets[start + len(needle) - 1] + 1
                    while left and source[left - 1] == "$":
                        left -= 1
                    while right < len(source) and source[right] == "$":
                        right += 1
                    matches.append(source[left:right])
                    start = normalized.find(needle, start + 1)
            if len(matches) == 1:
                evidence["quote"] = matches[0]
                repairs.append(
                    {
                        "original_quote": quote,
                        "literal_quote": matches[0],
                        "rule": "unique_whitespace_math_delimiters",
                    }
                )
    validate_extraction(value, assets, snapshot)
    return repairs
