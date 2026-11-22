"""共享ES召回载荷，不在基础设施决定题目访问规则或模型空间。"""

from copy import deepcopy


class RetrievalIndex:
    def __init__(self, search):
        self.search = search

    async def _query(self, index, limit, **body):
        return await self.search.request("POST", f"/{index}/_search", json={"size": limit,
            "_source": {"excludes": ["embedding"]}, **body})

    async def exact(self, index, scope, value, course_id, limit):
        boolean = deepcopy(scope["bool"])
        boolean.update(should=[{"ids": {"values": [value, course_id + ":" + value]}},
            {"term": {"source_id": value.removeprefix(course_id + ":")}}], minimum_should_match=1)
        return await self._query(index, limit, query={"bool": boolean})

    async def keyword(self, index, scope, query, limit, *, fields=("text",), course_id=None):
        boolean = deepcopy(scope["bool"])
        if query.strip():
            if fields == ("text",):
                boolean["must"] = [{"match": {"text": query}}]
            else:
                choices = [{"multi_match": {"query": query, "fields": list(fields), "type": "best_fields"}}]
                if course_id:
                    value = query.strip()
                    choices.extend([{"term": {"source_id": value}}, {"ids": {"values": [value, course_id + ":" + value]}}])
                boolean.update(should=choices, minimum_should_match=1)
        return await self._query(index, limit, query={"bool": boolean})

    async def vector(self, index, scope, embedding, limit):
        return await self._query(index, limit, knn={"field": "embedding", "query_vector": embedding,
            "k": limit, "num_candidates": max(100, limit), "filter": scope})
