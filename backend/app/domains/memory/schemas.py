"""候选只含正文与已有来源，不能写目标、作答或掌握度。"""

import math


def estimate_tokens(value: str) -> int:
    """无专用分词器时采用偏保守的估算，中文按每字两个 Token 预留。"""
    non_ascii = sum(ord(char) > 127 for char in value)
    return math.ceil((len(value) - non_ascii) / 2) + non_ascii * 2
