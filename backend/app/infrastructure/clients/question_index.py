"""按课程建立成对版本索引，使用一次别名操作发布。"""

import hashlib

from .search_analysis import IK_TEXT_ANALYSIS


class QuestionIndex:
    def __init__(self, search):
        self.search = search

    @staticmethod
    def aliases(course_id, namespace="main"):
        prefix = "stem_exercise_" + hashlib.sha256((course_id + ":" + namespace).encode()).hexdigest()[:12]
        return prefix + "_text", prefix + "_visual"

    async def ensure(self, index, dimensions):
        properties = {key: {"type": "keyword"} for key in ("course_id", "question_id", "source_id", "visibility", "concept_ids",
            "question_type", "source_hash", "projection_id", "representation_id", "config_hash", "duplicate_key")}
        properties.update(text={"type": "text", **IK_TEXT_ANALYSIS}, embedding={"type": "dense_vector", "dims": dimensions,
            "index": True, "similarity": "cosine"})
        result = await self.search.http.head("/" + index)
        if result.status_code == 404:
            await self.search.request("PUT", "/" + index, json={"settings": {"number_of_shards": 1, "number_of_replicas": 0}, "mappings": {"dynamic": "strict", "properties": properties}})
        else:
            result.raise_for_status()
            mapping = await self.search.request("GET", "/" + index + "/_mapping")
            current = mapping[index]["mappings"]["properties"]
            if current["embedding"]["dims"] != dimensions:
                raise ValueError("索引向量空间与构建配置不一致")
            if any(current["text"].get(k) != v for k, v in IK_TEXT_ANALYSIS.items()):
                raise ValueError("习题索引分词配置与构建配置不一致")

    async def publish(self, course_id, indices, namespace="main"):
        actions = []
        for alias, index in zip(self.aliases(course_id, namespace), indices, strict=True):
            existing = await self.search.http.get("/_alias/" + alias)
            if existing.status_code == 200:
                actions.extend({"remove": {"index": old, "alias": alias}} for old in existing.json())
            elif existing.status_code != 404:
                existing.raise_for_status()
            if index:
                actions.append({"add": {"index": index, "alias": alias}})
        if actions:
            await self.search.request("POST", "/_aliases", json={"actions": actions})

    async def withdraw(self, course_id, question_id):
        errors = []
        for alias in self.aliases(course_id):
            try:
                await self.search.request("POST", f"/{alias}/_delete_by_query", params={"refresh": "true"}, json={"query": {"term": {"question_id": question_id}}})
            except Exception as exc:
                errors.append(type(exc).__name__)
        return errors
