"""按内容版本恢复提取与构建，校验全部来源后发布索引对。"""

from app.domains.courses.questions.search.models import QuestionRepresentation, QuestionSearchProjection
from app.domains.courses.questions.search.representation import (
    REPRESENTATION_VERSION, public_snapshot, source_hash, digest, retrieval_text, text_views, duplicate_key,
)
from app.domains.courses.questions.search.schemas import VisualExtraction
from app.infrastructure.clients.question_index import QuestionIndex
from app.infrastructure.clients.search_analysis import IK_ANALYSIS_VERSION
from .assets import bind_assets
from .vision import PROMPT_VERSION
from .encoding import text_embedding


class QuestionSearchBuilder:
    def __init__(self, store, reader, vision, models, search, multimodal, namespace="main"):
        self.store, self.reader, self.vision = store, reader, vision
        self.models, self.search, self.multimodal = models, search, multimodal
        self.index = QuestionIndex(search)
        self.namespace = namespace

    def current(self, question):
        snapshot = public_snapshot(question)
        assets = bind_assets(self.store, self.reader, question)
        return snapshot, assets, source_hash(snapshot, assets)

    async def extract(self, identifier, course_id):
        q = self.store.question(identifier, course_id)
        snapshot, assets, fingerprint = self.current(q)
        extractor = self.models.settings.llm_model + ":" + PROMPT_VERSION
        key = digest([fingerprint, extractor, REPRESENTATION_VERSION])
        existing = self.store.representation(key)
        if existing:
            return existing
        if assets:
            payload, _ = await self.vision.extract(snapshot, assets)
            status = "pending"
        else:
            payload = VisualExtraction().model_dump()
            status = "native_only"
        self.store.insert(QuestionRepresentation, {"id": key, "question_id": q.id, "course_id": course_id,
            "source_hash": fingerprint, "asset_ids": [a["id"] for a in assets], "extractor": extractor,
            "payload": payload, "review_status": status, "review": {}})
        return self.store.representation(key)

    def config(self, visual):
        s = self.models.settings
        return {"namespace": self.namespace, "representation": REPRESENTATION_VERSION, "text_model": s.embedding_model, "text_dim": s.embedding_dim,
            "text_endpoint": s.embedding_url, "text_chunk_size": 10000, "text_analysis": IK_ANALYSIS_VERSION,
            "visual": self.multimodal.space if visual else None}

    async def build(self, course_id, identifiers=None, *, visual=False):
        identifier, token = self.store.claim_build(course_id, self.namespace)
        try:
            result = await self._build(course_id, identifiers, visual=visual, lease=(identifier, token))
            self.store.renew_build(identifier, token, finish=True, receipt={"status": "published", **result})
            return result
        except Exception as exc:
            self.store.renew_build(identifier, token, finish=True, receipt={"status": "failed", "error": type(exc).__name__})
            raise

    async def _build(self, course_id, identifiers=None, *, visual=False, lease):
        questions = self.store.questions(course_id)
        if identifiers:
            wanted = set(identifiers)
            questions = [q for q in questions if q.id in wanted]
            if {q.id for q in questions} != wanted:
                raise ValueError("构建列表包含非公开或其他课程题目")
        if not questions:
            raise ValueError("没有可构建题目")
        rows = []
        for q in questions:
            snapshot, assets, fingerprint = self.current(q)
            rep = self.store.selected_representation(q.id, fingerprint)
            text = retrieval_text(snapshot, rep.payload if rep else None, approved=bool(rep))
            # 纯图题缺少可靠描述时允许视觉通道；不能生成无意义的文字向量。
            rows.append({"question": q, "snapshot": snapshot, "assets": assets, "source_hash": fingerprint,
                "representation_id": rep.id if rep else "native", "text": text,
                "extraction_status": "approved" if rep else "native_only"})
        config = self.config(visual)
        generation = digest([course_id, config, [(r["question"].id, r["source_hash"], r["representation_id"]) for r in rows]])
        config_hash = digest(config)
        indices = [f"{a}_{generation[:16]}" for a in self.index.aliases(course_id, self.namespace)]
        await self.index.ensure(indices[0], config["text_dim"])
        if visual:
            await self.index.ensure(indices[1], self.multimodal.dimensions)
        visual_count = text_count = 0
        for row in rows:
            self.store.renew_build(*lease)
            q = row["question"]
            projection_id = digest([generation, q.id])
            common = {"question_id": q.id, "course_id": course_id, "source_id": q.source_id, "visibility": "public",
                "concept_ids": q.concept_ids, "question_type": q.question_type, "source_hash": row["source_hash"],
                "projection_id": projection_id, "representation_id": row["representation_id"], "config_hash": config_hash,
                "duplicate_key": duplicate_key(row["snapshot"], row["assets"])}
            texts = [t for t in text_views(row["text"]) if t.strip()]
            if texts:
                vectors, text_metrics = await text_embedding(self.models, texts)
                await self.search.bulk_index(indices[0], [{**common, "id": f"{q.id}:{i}", "text": text, "embedding": v}
                    for i, (text, v) in enumerate(zip(texts, vectors, strict=True))])
                text_count += len(texts)
            else:
                text_metrics = {}
            metrics = {}
            if visual and (row["text"] or row["assets"]):
                images = self.vision.pixels(row["assets"])
                vector, metrics = await self.multimodal.embed(row["text"], images)
                await self.search.bulk_index(indices[1], [{**common, "id": q.id, "text": row["text"], "embedding": vector}])
                visual_count += 1
            payload = {"namespace": self.namespace, "config": config, "indices": indices if visual else [indices[0], None], "text": row["text"],
                "full_text_hash": digest(row["text"]), "embedding_inputs": texts, "truncation": "none_split_by_10000_characters",
                "asset_ids": [a["id"] for a in row["assets"]], "extraction_status": row["extraction_status"],
                "duplicate_key": common["duplicate_key"], "text_metrics": text_metrics, "multimodal_metrics": metrics}
            self.store.insert(QuestionSearchProjection, {"id": projection_id, "question_id": q.id, "course_id": course_id,
                "representation_id": row["representation_id"], "source_hash": row["source_hash"], "generation": generation,
                "config_hash": config_hash, "payload": payload, "status": "ready"})
        for row in rows:
            if self.current(self.store.question(row["question"].id, course_id))[2] != row["source_hash"]:
                raise ValueError("构建中来源变化，拒绝发布")
        self.store.renew_build(*lease)
        await self.index.publish(course_id, [indices[0], indices[1] if visual else None], self.namespace)
        self.store.publish(course_id, generation, self.namespace, lease=lease)
        return {"generation": generation, "config": config, "questions": len(rows), "text_views": text_count,
                "visual_vectors": visual_count, "indices": indices if visual else [indices[0], None]}
