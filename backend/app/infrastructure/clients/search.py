"""ES 接口保持独立，BM25 与 kNN 均执行相同范围过滤。"""

import json

import httpx

from app.infrastructure.config import get_settings
from .search_analysis import IK_TEXT_ANALYSIS


class SearchClient:
    MATERIALS = "stem_tutor_materials"
    QUESTIONS = "stem_tutor_questions"
    LEGACY_INDICES = {MATERIALS: "stem_tutor_materials_v1", QUESTIONS: "stem_tutor_questions_v1"}

    def __init__(self) -> None:
        settings = get_settings()
        auth = (settings.es_user, settings.es_password) if settings.es_user else None
        self.http = httpx.AsyncClient(
            base_url=settings.es_url, auth=auth, timeout=25, trust_env=False
        )
        self.dimensions = settings.embedding_dim

    async def request(self, method: str, path: str, **kwargs) -> dict:
        response = await self.http.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    async def ensure_indices(self) -> None:
        properties = {
            "course_id": {"type": "keyword"},
            "concept_ids": {"type": "keyword"},
            "source_id": {"type": "keyword"},
            "question_type": {"type": "keyword"},
            "cognitive_type": {"type": "keyword"},
            "visibility": {"type": "keyword"},
            "question_id": {"type": "keyword"},
            "source_ref": {"type": "keyword", "index": False},
            "origin": {"type": "keyword"},
            "version": {"type": "integer"},
            "title": {"type": "text", **IK_TEXT_ANALYSIS},
            "text": {
                "type": "text",
                **IK_TEXT_ANALYSIS,
                "fields": {"raw": {"type": "keyword", "ignore_above": 1000}},
            },
            "embedding": {
                "type": "dense_vector",
                "dims": self.dimensions,
                "index": True,
                "similarity": "cosine",
                "index_options": {"type": "int8_hnsw"},
            },
        }
        for alias, legacy in self.LEGACY_INDICES.items():
            response = await self.http.head("/_alias/" + alias)
            if response.status_code == 404:
                old = await self.http.head("/" + legacy)
                if old.status_code != 404:
                    old.raise_for_status()
                    raise RuntimeError("检测到旧版 ES 索引，请先运行 migrate_es_ik.py 迁移。")
                await self.request(
                    "PUT",
                    "/" + alias + "_ik_initial",
                    json={
                        "settings": {"number_of_shards": 1, "number_of_replicas": 0},
                        "mappings": {"properties": properties},
                        "aliases": {alias: {}},
                    },
                )
            else:
                response.raise_for_status()

    async def bulk_index(
        self, index: str, documents: list[dict], *, refresh: bool = True
    ) -> None:
        for offset in range(0, len(documents), 50):
            payload = []
            for document in documents[offset : offset + 50]:
                document = document.copy()
                identifier = document.pop("id")
                payload.extend(
                    [
                        json.dumps({"index": {"_index": index, "_id": identifier}}),
                        json.dumps(document, ensure_ascii=False),
                    ]
                )
            result = await self.request(
                "POST",
                "/_bulk",
                content="\n".join(payload) + "\n",
                headers={"Content-Type": "application/x-ndjson"},
            )
            if result.get("errors"):
                errors = [
                    item["index"].get("error", {}).get("type")
                    for item in result["items"]
                    if "error" in item["index"]
                ]
                raise RuntimeError(f"ES 批量索引失败：{errors[:3]}")
        if refresh:
            await self.request("POST", f"/{index}/_refresh")

    async def close(self) -> None:
        await self.http.aclose()
