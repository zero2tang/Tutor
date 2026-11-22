"""阿里云图文融合编码，明确与现有文本向量空间隔离。"""

import json
import math
import time
from pathlib import Path
from app.infrastructure.config import ROOT


class MultimodalEmbeddingClient:
    def __init__(self, models, settings):
        self.models, self.settings = models, settings
        self.model = settings.exercise_mm_model
        self.dimensions = settings.exercise_mm_dim
        self.url = settings.exercise_mm_url
        self.cache = ROOT / "data/cache/exercise_multimodal_vectors"

    @property
    def space(self):
        return {"model": self.model, "dimensions": self.dimensions, "endpoint": self.url, "fusion": True, "version": "qwen-fusion-v1"}

    async def embed(self, text, images):
        import hashlib
        if self.model != "qwen3-vl-embedding" or self.dimensions not in {256, 512, 768, 1024, 1536, 2048, 2560}:
            raise ValueError("当前适配器仅支持已核对融合契约的 qwen3-vl-embedding")
        if not text and not images:
            raise ValueError("图文向量输入为空")
        if len(images) > 10 or len(text) > 20000:
            raise ValueError("图文输入超过试点预算，拒绝静默截断")
        if len(images) > 1 or any(i.get("role_label", "stem") != "stem" for i in images):
            # 多图或选项图必须保留图片与题干/选项的关联，不把路径字符串当内容。
            text += "\n配图顺序：" + "；".join(f"图{n}: {i.get('role_label', '公开题图')}" for n, i in enumerate(images, 1))
        key = hashlib.sha256(json.dumps([self.space, text, [i["hash"] for i in images]], sort_keys=True).encode()).hexdigest()
        path = self.cache / (key + ".json")
        def valid(v):
            return isinstance(v, list) and len(v) == self.dimensions and all(isinstance(x, (int, float)) and math.isfinite(x) for x in v) and any(v)
        if path.exists():
            value = json.loads(path.read_text(encoding="utf-8"))
            if valid(value):
                return value, {"cache_hit": True, "requests": 0, "space": self.space}
        contents = ([{"text": text}] if text else []) + [{"image": i["url"]} for i in images]
        from app.infrastructure.clients.llm_metrics import ModelRequestMetrics
        metrics = ModelRequestMetrics()
        result = await self.models._post(self.url, self.settings.embedding_key,
            {"model": self.model, "input": {"contents": contents}, "parameters": {"enable_fusion": True, "dimension": self.dimensions}}, metrics=metrics)
        values = result.get("output", {}).get("embeddings", [])
        if len(values) != 1 or not valid(values[0].get("embedding")):
            raise ValueError("融合向量数量、维度或数值异常")
        vector = values[0]["embedding"]
        self.cache.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(f".{time.time_ns()}.tmp")
        temp.write_text(json.dumps(vector), encoding="utf-8")
        temp.replace(path)
        return vector, {"cache_hit": False, "usage": result.get("usage", {}), "request_metrics": metrics.snapshot({"model": self.model, "thinking": {"type": "disabled"}, "stream": False}, result.get("usage")), "space": self.space}
