"""只调用声明的召回通道，不调用业务检索的短路、重排或降级。"""

import asyncio
import time
from pathlib import Path
from app.infrastructure.clients.search import SearchClient
from app.infrastructure.clients.question_index import QuestionIndex
from app.infrastructure.clients.retrieval_index import RetrievalIndex
from app.application.question_retrieval.encoding import text_embedding
from app.domains.courses.retrieval.fusion import fuse
from app.domains.courses.questions.search.representation import text_views
from .ai import Clients
from .files import digest, read_rows, write_json, read_json, append_row, pin
from .config import model_config, experiment_hash


def representation_hash(corpus):
    return digest(
        [
            {k: r[k] for k in ("id", "text", "source_hash", "duplicate_key")}
            for r in corpus
        ]
    )


class Retriever:
    def __init__(self, dataset, *, clients=None, search=None):
        self.dataset = Path(dataset)
        self.corpus = read_rows(self.dataset / "corpus.jsonl")
        self.lookup = {r["id"]: r for r in self.corpus}
        self.clients = clients or Clients(dataset)
        self.search = search or SearchClient()
        self.index = RetrievalIndex(self.search)
        self.config = {
            "corpus_hash": representation_hash(self.corpus),
            "models": model_config(self.clients.models.settings),
            "chunk_size": 10000,
            "experiment_hash": experiment_hash(),
            "analyzer": "cjk",
        }
        self.fingerprint = digest(self.config)
        self.indices = [
            "benchmark_retrieval_" + self.fingerprint[:20] + suffix
            for suffix in ("_text", "_visual")
        ]
        self.fetch_limit = 50 * max(
            (len(text_views(r["text"])) for r in self.corpus), default=1
        )

    async def close(self):
        await asyncio.gather(self.clients.close(), self.search.close())

    async def vectors(self, query, channels):
        values, usage = {}, {}
        if "text_vector" in channels:
            values["text_vector"], usage["text"] = await asyncio.wait_for(
                text_embedding(self.clients.models, text_views(query["text"])),
                timeout=180,
            )
        if "multimodal" in channels:
            images = self.clients.query_vision.pixels(query["assets"])
            values["multimodal"], usage["multimodal"] = await asyncio.wait_for(
                self.clients.mm.embed(query["text"], images), timeout=180
            )
        if usage:
            append_row(
                self.dataset / "work/encoding_usage.jsonl",
                {"id": query["id"], "phase": "query", "usage": usage},
            )
        return values

    def require_index(self):
        state = read_json(self.dataset / "index.json")
        if (
            not state
            or state.get("status") != "ready"
            or state.get("fingerprint") != self.fingerprint
        ):
            raise ValueError("索引尚未完成或与冻结文本、模型、实验配置不兼容")

    async def retrieve(self, query, experiment, vectors=None):
        self.require_index()
        start = time.perf_counter()
        if vectors is None:
            vectors = await self.vectors(query, experiment.channels)
        excluded = set(query.get("excluded_ids", []))
        scope = {
            "bool": {
                "filter": [
                    {"term": {"course_id": "math"}},
                    {"term": {"visibility": "public"}},
                ],
                "must_not": (
                    [{"terms": {"question_id": sorted(excluded)}}] if excluded else []
                ),
            }
        }

        async def channel(name):
            if name == "keyword":
                result = await self.index.keyword(
                    self.indices[0], scope, query["text"], self.fetch_limit
                )
                hits = result["hits"]["hits"]
            elif name == "text_vector":
                hits = []
                for vector in vectors["text_vector"]:
                    result = await self.index.vector(
                        self.indices[0], scope, vector, self.fetch_limit
                    )
                    hits.extend(result["hits"]["hits"])
                hits.sort(key=lambda h: (-(h.get("_score") or 0), h["_id"]))
            elif name == "multimodal":
                result = await self.index.vector(
                    self.indices[1], scope, vectors[name], experiment.candidate_limit
                )
                hits = result["hits"]["hits"]
            else:
                raise ValueError("未声明的召回通道")
            result, seen = [], set()
            for hit in hits:
                source = hit["_source"]
                qid = source["question_id"]
                if qid in seen:
                    continue
                row = self.lookup.get(qid)
                if (
                    not row
                    or qid in excluded
                    or source.get("source_hash") != row["source_hash"]
                ):
                    raise ValueError("召回了快照外、过期或排除范围内的题目")
                seen.add(qid)
                result.append(hit)
                if len(result) == experiment.candidate_limit:
                    break
            return result

        # gather发生异常时整组失败，绝不把缺失通道结果冒充完整对照。
        results = await asyncio.gather(*(channel(name) for name in experiment.channels))
        channels = dict(zip(experiment.channels, results, strict=True))
        ranked = fuse(channels, k=experiment.rrf_k)
        return {
            "status": "completed",
            "channels_used": list(channels),
            "rankings": [
                {
                    "id": r["id"],
                    "score": (
                        next(iter(r["raw_scores"].values()))
                        if len(channels) == 1
                        else r["rrf_score"]
                    ),
                    "ranks": r["ranks"],
                    "raw_scores": r["raw_scores"],
                }
                for r in ranked
            ],
            "channel_rankings": {
                name: [
                    {"id": h["_source"]["question_id"], "score": h.get("_score")}
                    for h in hits
                ]
                for name, hits in channels.items()
            },
            "candidate_count": len(ranked),
            "retrieval_ms": (time.perf_counter() - start) * 1000,
            "fingerprint": self.fingerprint,
            "input_hash": digest(
                [
                    query["text"],
                    [a["content_hash"] for a in query["assets"]],
                    query.get("excluded_ids", []),
                ]
            ),
        }


async def build_index(dataset, *, limit=None):
    retriever = Retriever(dataset)
    try:
        if any(r["transcription_status"] == "pending" for r in retriever.corpus):
            raise ValueError("图示转录未完成，不能在各组混用不同表示")
        helper = QuestionIndex(retriever.search)
        for index, dim in zip(
            retriever.indices,
            (
                retriever.clients.models.settings.embedding_dim,
                retriever.clients.mm.dimensions,
            ),
            strict=True,
        ):
            if not index.startswith("benchmark_retrieval_"):
                raise ValueError("拒绝写入非评测索引")
            await helper.ensure(index, dim)
        pin(
            Path(dataset) / "work" / retriever.fingerprint / "config.json",
            retriever.config,
        )
        rows = retriever.corpus[:limit] if limit else retriever.corpus
        for row in rows:
            texts = text_views(row["text"])
            vectors, text_usage = await asyncio.wait_for(
                text_embedding(retriever.clients.models, texts), timeout=180
            )
            images = retriever.clients.document_vision.pixels(row["assets"])
            visual, visual_usage = await asyncio.wait_for(
                retriever.clients.mm.embed(row["text"], images), timeout=180
            )
            common = {
                "question_id": row["id"],
                "course_id": "math",
                "visibility": "public",
                "source_hash": row["source_hash"],
                "duplicate_key": row["duplicate_key"],
                "config_hash": retriever.fingerprint,
            }
            await retriever.search.bulk_index(
                retriever.indices[0],
                [
                    {**common, "id": f"{row['id']}:{i}", "text": t, "embedding": v}
                    for i, (t, v) in enumerate(zip(texts, vectors, strict=True))
                ],
                refresh=False,
            )
            await retriever.search.bulk_index(
                retriever.indices[1],
                [{**common, "id": row["id"], "text": row["text"], "embedding": visual}],
                refresh=False,
            )
            append_row(
                Path(dataset) / "work/encoding_usage.jsonl",
                {
                    "id": row["id"],
                    "phase": "index",
                    "usage": {"text": text_usage, "multimodal": visual_usage},
                },
            )
            print("indexed " + row["id"], flush=True)
        for index in retriever.indices:
            await retriever.search.request("POST", f"/{index}/_refresh")
        actual = [
            (await retriever.search.request("GET", f"/{index}/_count"))["count"]
            for index in retriever.indices
        ]
        expected = [
            sum(len(text_views(r["text"])) for r in retriever.corpus),
            len(retriever.corpus),
        ]
        result = {
            "status": "ready" if actual == expected else "partial",
            "fingerprint": retriever.fingerprint,
            "indices": retriever.indices,
            "config": retriever.config,
            "counts": actual,
            "expected": expected,
        }
        write_json(Path(dataset) / "index.json", result)
        return result
    finally:
        await retriever.close()
