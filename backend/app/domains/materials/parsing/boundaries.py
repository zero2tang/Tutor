"""仅用明确的解答标签识别内容边界，不根据答案值删除题干文字。"""

import re

MARKER = r"(?:参考答案|标准答案|答案|解答|解析|Solution|Answer|答|解)\s*[：:]"


def starts_solution_marker(text: str) -> bool:
    return bool(re.match(r"^\s*" + MARKER, text, re.I))


def split_solution_marker(text: str):
    protected = [
        match.span()
        for match in re.finditer(
            r"\$\$(.+?)\$\$|(?<!\\)\$([^$\n]+?)(?<!\\)\$|\\\((.+?)\\\)|\\\[(.+?)\\\]|`[^`]*`",
            text,
            re.S,
        )
    ]
    for match in re.finditer(
        r"(?<![A-Za-z\u3400-\u9fff])" + MARKER,
        text,
        re.I,
    ):
        if not text[: match.start()].strip() or any(
            start <= match.start() < end for start, end in protected
        ):
            continue
        return (
            text[: match.start()].rstrip(),
            text[match.start() :].lstrip(),
            match.start(),
        )
    return None
