"""通过回环地址调用独立的 Qwen 精排进程。"""

import math
from urllib.parse import urlsplit

import httpx


class LocalReranker:
    def __init__(self, url):
        address = urlsplit(url)
        if (address.scheme != "http" or address.netloc != f"127.0.0.1:{address.port}"
                or address.path not in {"", "/"} or address.query or address.fragment):
            raise ValueError("精排服务必须使用本机 127.0.0.1 和显式端口")
        self.url = url.rstrip("/")

    async def score(self, query, candidates, intent):
        visual = bool(query["images"] or any(doc["images"] for doc in candidates))
        expected_model = "Qwen3-VL-Reranker-2B" if visual else "Qwen3-Reranker-0.6B"
        async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(10.0, connect=1.0)) as client:
            response = await client.post(self.url + "/rerank",
                json={"intent": intent, "query": query, "candidates": candidates})
            response.raise_for_status()
            result = response.json()
        rows = result["scores"]
        scores = {row["id"]: row["score"] for row in rows}
        expected_ids = {doc["id"] for doc in candidates}
        expected_images = len(query["images"]) * len(candidates) + sum(len(doc["images"]) for doc in candidates)
        if (result["model"] != expected_model or len(rows) != len(expected_ids) or scores.keys() != expected_ids
                or result["images_scored"] != expected_images
                or any(type(score) not in (int, float) or not math.isfinite(score) for score in scores.values())):
            raise ValueError("精排响应的模型、候选或图像数量不匹配")
        return scores, {"model": expected_model, "candidate_count": len(candidates),
                        "elapsed_ms": result["elapsed_ms"], "images_scored": expected_images,
                        "max_tokens": result["max_tokens"]}
