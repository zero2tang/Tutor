"""只负责完整原答的有来源分块，不形成记忆或掌握结论。"""

import json
from .schemas import estimate_tokens


def fact_chunks(facts: list[dict], token_limit: int = 2800) -> list[list[dict]]:
    """超长单条事实按有来源的文本片段读取，不改写数据库中的原始答案。"""
    chunks, batch, tokens = [], [], 0
    for fact in facts:
        payload = json.dumps(fact["payload"], ensure_ascii=False)
        # 最坏全中文约两 Token/字，片段上限小于单批上限。
        fragments = [payload[i : i + 900] for i in range(0, len(payload), 900)] or [""]
        for index, fragment in enumerate(fragments):
            value = {
                "source_ref": fact["source_ref"],
                "seq": fact["seq"],
                "kind": fact["kind"],
                "fragment": fragment,
                "fragment_index": index + 1,
                "fragment_count": len(fragments),
            }
            size = estimate_tokens(json.dumps(value, ensure_ascii=False))
            if batch and tokens + size > token_limit:
                chunks.append(batch)
                batch, tokens = [], 0
            batch.append(value)
            tokens += size
    if batch:
        chunks.append(batch)
    return chunks or [[]]
