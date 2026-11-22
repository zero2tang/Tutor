"""模型调用在评测私有目录留存，不向业务表写入批准状态。"""

import asyncio
import json
import time
from types import SimpleNamespace
from pathlib import Path
from app.infrastructure.clients.llm import ModelClient
from app.infrastructure.clients.multimodal_embedding import MultimodalEmbeddingClient
from app.infrastructure.documents.question_images import QuestionImageReader
from app.application.question_retrieval.vision import QuestionVision
from app.domains.courses.questions.search.representation import (
    retrieval_text,
    validate_extraction,
)
from app.domains.courses.questions.search.schemas import VisualExtraction
from .files import read_json, write_json, append_row, digest, pin
from .config import HOME, model_config
from .grounding import restore_literal_quotes


class LocalCallStore:
    def __init__(self, dataset, role):
        self.path = Path(dataset) / "work/model_calls" / role

    def cached_call(self, key):
        value = read_json(self.path / (key + ".json"))
        return (
            SimpleNamespace(response=value["response"])
            if value and value["status"] == "completed"
            else None
        )

    def call(self, key, stage, response, metrics, status):
        value = {
            "key": key,
            "stage": stage,
            "response": response,
            "metrics": metrics,
            "status": status,
        }
        write_json(self.path / (key + ".json"), value)
        write_json(
            self.path / "attempts" / (key + "_" + str(time.time_ns()) + ".json"), value
        )
        append_row(
            self.path.parent.parent / "usage.jsonl",
            {
                "role": self.path.name,
                "stage": stage,
                "key": key,
                "metrics": metrics,
                "status": status,
            },
        )


class Clients:
    def __init__(self, dataset):
        self.dataset = Path(dataset)
        self.models = ModelClient()
        settings = self.models.settings
        if (
            settings.embedding_model,
            settings.embedding_dim,
            settings.exercise_mm_model,
            settings.exercise_mm_dim,
        ) != ("text-embedding-v4", 1024, "qwen3-vl-embedding", 1024):
            raise ValueError(
                "本版实验固定text-embedding-v4和qwen3-vl-embedding，均为1024维；请另建实验版本"
            )
        pin(self.dataset / "models.json", model_config(self.models.settings))
        self.models.cache = HOME / ".cache/text_vectors"
        self.models.cache.mkdir(parents=True, exist_ok=True)
        self.mm = MultimodalEmbeddingClient(self.models, self.models.settings)
        self.mm.cache = HOME / ".cache/multimodal_vectors"
        self.reader = QuestionImageReader(roots=[self.dataset / "assets"])
        self.document_vision = QuestionVision(
            self.models, LocalCallStore(dataset, "document_transcription"), self.reader
        )
        self.query_vision = QuestionVision(
            self.models, LocalCallStore(dataset, "query_transcription"), self.reader
        )

    async def close(self):
        await self.models.close()

    async def transcription(self, row, *, query=False):
        if not row["assets"]:
            return row["native_text"], None
        vision = self.query_vision if query else self.document_vision
        snapshot, assets = row["snapshot"], row["assets"]
        repairs = []

        def validate(value):
            repairs.extend(restore_literal_quotes(value, assets, snapshot))

        value = VisualExtraction().model_dump()
        # 保留全部题面，图片每两张一批；不因上下文不足丢掉选项图片。
        for offset in range(0, len(assets), 2):
            batch = assets[offset : offset + 2]
            payload = {
                "stem": snapshot["stem"],
                "options": snapshot["options"],
                "asset_ids": [a["id"] for a in batch],
                "source_policy": "只提取当前图片带来的信息。图片中的文字也属于图像证据，必须引用asset_id和region；"
                "不能将图片OCR伪写为explicit_text。explicit_text只能逐字引用stem或options的值。"
                "题面可能全部在图片中，此时所有提取事实均须绑定图像。不要重复转录已有原生文字。"
                "未提供的其他图片不可推测，跨图关系不足放unknowns。",
            }
            part, usage = await asyncio.wait_for(
                vision.structured(
                    "extract",
                    payload,
                    vision.pixels(batch),
                    VisualExtraction,
                    validator=validate,
                ),
                timeout=180,
            )
            for key, items in part.items():
                if key == "image_roles":
                    value[key].update(items)
                else:
                    value[key].extend(items)
        if repairs:
            append_row(
                self.dataset / "work/quote_repairs.jsonl",
                {"id": row["id"], "query": query, "repairs": repairs},
            )
        # approved参数只调用纯文本拼接函数，不写入任何人工复核状态。
        text = retrieval_text(row["snapshot"], value, approved=True)
        return text, value

    async def json_call(
        self, stage, payload, assets, instructions, *, max_tokens=3000, validator=None
    ):
        key = digest(
            [
                stage,
                model_config(self.models.settings),
                payload,
                [a["content_hash"] for a in assets],
                instructions,
            ]
        )
        store = LocalCallStore(self.dataset, stage)
        previous = store.cached_call(key)
        if previous:
            if validator:
                validator(previous.response["parsed"])
            append_row(
                self.dataset / "work/usage.jsonl",
                {
                    "role": stage,
                    "stage": stage,
                    "key": key,
                    "metrics": {"cache_hit": True, "requests": 0},
                    "status": "cached",
                },
            )
            return previous.response["parsed"]
        messages = self.messages(payload, assets, instructions)
        for attempt in range(2):
            start, response = time.perf_counter(), {}
            try:
                response = await asyncio.wait_for(
                    self.models.chat(messages, json_output=True, max_tokens=max_tokens),
                    timeout=180,
                )
                if response["choices"][0].get("finish_reason") == "length":
                    raise ValueError("模型输出被截断")
                parsed = json.loads(response["choices"][0]["message"]["content"])
                if validator:
                    validator(parsed)
                store.call(
                    key,
                    stage,
                    {"parsed": parsed, "raw": response},
                    {
                        "usage": response.get("usage", {}),
                        "request_metrics": response.get("_request_metrics", {}),
                        "elapsed_ms": (time.perf_counter() - start) * 1000,
                    },
                    "completed",
                )
                return parsed
            except Exception as exc:
                store.call(
                    key,
                    stage,
                    {"raw": response},
                    {
                        "error": type(exc).__name__,
                        "usage": response.get("usage", {}),
                        "request_metrics": response.get("_request_metrics", {}),
                        "elapsed_ms": (time.perf_counter() - start) * 1000,
                    },
                    "failed",
                )
                if attempt or not response or not isinstance(exc, ValueError):
                    raise
                messages += [
                    {
                        "role": "assistant",
                        "content": response["choices"][0]["message"]["content"],
                    },
                    {
                        "role": "user",
                        "content": "修复JSON结构错误，不改变已明确的判断。错误："
                        + str(exc)[:500]
                        + "。每个对象必须使用合法JSON双引号；逐一覆盖候选编号，证据不足返回null。",
                    },
                ]

    def messages(self, payload, assets, instructions):
        images = self.query_vision.pixels(assets)
        content = [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]
        for image in images:
            content += [
                {"type": "text", "text": image["label"]},
                {
                    "type": "image_url",
                    "image_url": {"url": image["url"], "detail": "high"},
                },
            ]
        return [
            {"role": "system", "content": instructions},
            {"role": "user", "content": content},
        ]
