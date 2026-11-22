"""统一的含图习题检索编排，版本与访问检查覆盖所有召回通道。"""

import time
from sqlalchemy import select
from app.domains.courses.catalog import CatalogService, public_question
from app.domains.courses.models import Question
from app.domains.courses.retrieval.fusion import fuse
from app.domains.courses.retrieval.scope import search_scope, matches_scope
from app.domains.courses.retrieval.channels import run_channels
from app.infrastructure.clients.retrieval_index import RetrievalIndex
from app.domains.courses.questions.search.representation import REPRESENTATION_VERSION, public_snapshot, source_hash, duplicate_key, retrieval_text, text_views, digest
from app.domains.courses.questions.search.schemas import VisualExtraction
from app.domains.courses.questions.search.store import QuestionSearchStore
from app.domains.sessions.models import LearningSession
from app.domains.assessment.models import Attempt
from app.infrastructure.clients.question_index import QuestionIndex
from app.infrastructure.clients.multimodal_embedding import MultimodalEmbeddingClient
from app.infrastructure.documents.question_images import QuestionImageReader
from .vision import QuestionVision
from .rerank import rerank_exercises
from .assets import bind_assets
from .encoding import text_embedding


class QuestionRetrievalService:
    def __init__(self, models, search, *, store=None, reader=None, multimodal=None, namespace=None):
        self.models, self.search = models, search
        self.index = RetrievalIndex(search)
        self.store = store or QuestionSearchStore()
        self.reader = reader or QuestionImageReader()
        self.multimodal = multimodal or MultimodalEmbeddingClient(models, models.settings)
        self.vision = QuestionVision(models, self.store, self.reader)
        self.settings = models.settings
        self.namespace = namespace or getattr(self.settings, "exercise_search_namespace", "main")

    def enabled(self, course_id):
        return course_id in self.settings.exercise_search_courses.split(",")

    def scope(self, course_id, session_id, student_id, exclusions):
        denied = set(exclusions)
        if session_id:
            with self.store.database() as db:
                session = db.get(LearningSession, session_id)
                if not session or session.course_id != course_id or session.student_id != student_id:
                    raise ValueError("检索范围与当前学习会话不一致")
                denied.update(db.scalars(select(Attempt.question_id).where(Attempt.session_id == session_id)).all())
        keys = set()
        for qid in denied:
            try:
                q = self.store.question(qid, course_id, include_withdrawn=True)
                keys.add(duplicate_key(public_snapshot(q), bind_assets(self.store, self.reader, q)))
            except ValueError:
                pass
        return denied, keys

    async def retrieve(self, query, *, course_id, concept_ids=None, top_k=6, mode="hybrid", coverage="any",
                       question_type=None, excluded_question_ids=None, intent="isomorphic", asset_ids=None,
                       source_question_id=None, session_id=None, student_id="local", pipeline=None, candidate_question_ids=None):
        if intent not in {"exact", "isomorphic", "same_method"} or mode not in {"keyword", "hybrid"} or coverage not in {"any", "all"}:
            raise ValueError("检索意图或范围参数无效")
        if pipeline not in {None, "A", "B", "C", "D"}:
            raise ValueError("评测组别无效")
        CatalogService().course(course_id)
        concepts = concept_ids or []
        for cid in concepts:
            CatalogService().concept(cid, course_id)
        limit = max(1, min(int(top_k), 20))
        candidate_count = max(limit, min(max(self.settings.exercise_candidates, 1), 100))
        denied, excluded_keys = self.scope(course_id, session_id, student_id, excluded_question_ids or [])
        universe = set(candidate_question_ids) if candidate_question_ids is not None else None
        start = time.perf_counter()
        timings, usage, errors, channels, matches = {}, {}, [], {}, {}
        query_assets = []
        extraction_status = "text_only"
        semantic_query = query.strip()
        rerank_query = semantic_query

        def allowed(q):
            return matches_scope(q, concept_ids=concepts, coverage=coverage,
                question_type=question_type, exclusions=denied, universe=universe)

        # 题号确定性查找早于查询图片读取、向量和模型调用。
        exact = []
        if query.strip():
            value = query.strip()
            with self.store.database() as db:
                rows = db.scalars(select(Question).where(Question.course_id == course_id,
                    (Question.id.in_([value, course_id + ":" + value])) | (Question.source_id == value.removeprefix(course_id + ":")))).all()
            for q in rows:
                try:
                    q = self.store.question(q.id, course_id)
                except ValueError:
                    continue
                if allowed(q):
                    assets = bind_assets(self.store, self.reader, q)
                    if duplicate_key(public_snapshot(q), assets) not in excluded_keys:
                        exact.append(self._item(q, retrieval_text(public_snapshot(q)), assets, {"exact_id": 1}, 1, "exact_id"))
        if exact:
            return self._finish(exact[:limit], course_id, session_id, intent, {"exact_id": [i["id"] for i in exact]}, timings, usage, errors, start, "not_required")

        if asset_ids and not source_question_id:
            raise ValueError("查询资产必须关联当前课程的公开题目；此入口尚不支持上传")
        if source_question_id:
            source = self.store.question(source_question_id, course_id)
            all_assets = bind_assets(self.store, self.reader, source)
            if not all_assets and public_question(source).get("availability") == "missing_figure":
                errors.append("query_image:missing_asset")
                extraction_status = "missing_asset"
            if set(asset_ids or []) - {a["id"] for a in all_assets}:
                raise ValueError("查询图片不属于指定题目")
            query_assets = [a for a in all_assets if not asset_ids or a["id"] in asset_ids]
            if intent != "exact":
                denied.add(source.id)
                excluded_keys.add(duplicate_key(public_snapshot(source), all_assets))
            semantic_query = "\n".join(filter(None, [query.strip(), retrieval_text(public_snapshot(source))]))
            rerank_query = semantic_query
            if query_assets and pipeline != "A":
                t = time.perf_counter()
                try:
                    approved = self.store.selected_representation(source.id, source_hash(public_snapshot(source), all_assets))
                    if approved and (not asset_ids or set(asset_ids) == set(approved.asset_ids)):
                        payload, usage["query_extraction"] = approved.payload, {"reviewed_representation_reuse": approved.id, "requests": 0}
                    else:
                        payload, usage["query_extraction"] = await self.vision.extract(public_snapshot(source), query_assets)
                    # 查询读图是待确认的召回提示，不作为数据库确定条件或硬过滤。
                    semantic_query += "\n" + "\n".join(f["text"] for k in ("ocr", "formulas", "conditions", "goals") for f in payload[k] if f["evidence"]["evidence_kind"] != "unknown")
                    extraction_status = "model_query_hint"
                except Exception as exc:
                    errors.append("query_image:" + type(exc).__name__)
                    extraction_status = "failed"
                timings["query_extraction_ms"] = round((time.perf_counter() - t) * 1000, 2)
        projections = {p.question_id: p for p in self.store.projections(course_id, namespace=self.namespace)} if pipeline != "A" else {}
        if pipeline in {"B", "C", "D"} and not projections:
            errors.append("enhanced_index_not_published")
        text_index, visual_index = QuestionIndex.aliases(course_id, self.namespace) if projections else (self.search.QUESTIONS, None)
        scope = search_scope(course_id, concepts, coverage, question_type, denied,
            questions=True, universe=universe)
        visual_enabled = pipeline in {"C", "D"} or (pipeline is None and self.settings.exercise_mm_enabled and intent in getattr(self.settings, "exercise_mm_intents", "exact,isomorphic,same_method").split(","))
        rerank_enabled = pipeline == "D" or (pipeline is None and self.settings.exercise_rerank_enabled and intent in getattr(self.settings, "exercise_rerank_intents", "exact,isomorphic,same_method").split(","))

        async def keyword():
            return await self.index.keyword(text_index, scope, semantic_query, candidate_count)

        async def vector():
            if projections:
                configs = {digest(p.payload["config"]) for p in projections.values()}
                example = next(iter(projections.values())).payload["config"]
                if len(configs) != 1 or example["text_model"] != self.settings.embedding_model or example["text_dim"] != self.settings.embedding_dim or example["text_endpoint"] != self.settings.embedding_url:
                    raise ValueError("文本查询编码器与索引版本不兼容")
            vectors, usage["text_embedding"] = await text_embedding(self.models, text_views(semantic_query))
            responses = []
            for embedding in vectors:
                value = await self.index.vector(text_index, scope, embedding, candidate_count)
                responses.extend(value["hits"]["hits"])
            responses.sort(key=lambda h: -(h.get("_score") or 0))
            return {"hits": {"hits": responses}}

        async def visual():
            if not projections or not visual_index:
                raise ValueError("尚无已发布图文索引")
            if any(p.payload["config"]["visual"] != self.multimodal.space for p in projections.values()):
                raise ValueError("图文查询编码器与索引版本不兼容")
            embedding, usage["multimodal_embedding"] = await self.multimodal.embed(semantic_query, self.vision.pixels(query_assets))
            return await self.index.vector(visual_index, scope, embedding, candidate_count)

        work = {}
        if semantic_query.strip() or not query_assets:
            work["keyword"] = keyword
            if semantic_query.strip() and mode == "hybrid":
                work["text_vector"] = vector
        if visual_enabled and mode == "hybrid" and (semantic_query.strip() or query_assets):
            work["multimodal"] = visual
        if not work:
            errors.append("no_usable_query:图片无法读取且没有有效题意")
        channels = await run_channels(work, timings, errors)

        # 先从主库核对每个通道，再融合，过期候选不能占用最终名次。
        verified, candidates, cache = {}, {}, {}
        for name, hits in channels.items():
            verified[name] = []
            for hit in hits:
                row = hit.get("_source", {})
                qid = row.get("question_id") or hit["_id"]
                if qid not in cache:
                    try:
                        q = self.store.question(qid, course_id)
                        assets = bind_assets(self.store, self.reader, q)
                        fingerprint = source_hash(public_snapshot(q), assets)
                        key = duplicate_key(public_snapshot(q), assets)
                        cache[qid] = (q, assets, fingerprint, key) if allowed(q) and key not in excluded_keys else None
                    except ValueError:
                        cache[qid] = None
                current = cache[qid]
                if current is None:
                    continue
                q, assets, fingerprint, key = current
                p = projections.get(qid)
                if projections and (not p or row.get("projection_id") != p.id or row.get("source_hash") != fingerprint or p.source_hash != fingerprint
                    or p.payload["config"].get("representation") != REPRESENTATION_VERSION):
                    errors.append("stale_projection:" + qid)
                    continue
                if not projections and row.get("version", q.version) != q.version:
                    continue
                verified[name].append(hit)
                candidates[qid] = (q, assets, p)
        t = time.perf_counter()
        ranked = fuse(verified)
        timings["fusion_ms"] = round((time.perf_counter() - t) * 1000, 2)
        items, seen_keys = [], set()
        for record in ranked:
            q, assets, p = candidates[record["id"]]
            key = cache[q.id][3]
            if key in seen_keys:
                continue
            seen_keys.add(key)
            item = self._item(q, p.payload["text"] if p else retrieval_text(public_snapshot(q)), assets, record["ranks"], record["rrf_score"], "candidate")
            item["raw_scores"] = record["raw_scores"]
            item["extraction_status"] = p.payload["extraction_status"] if p else "native_only"
            if p:
                item["resource_version"].update(projection_id=p.id, representation_id=p.representation_id, generation=p.generation)
            items.append(item)
        if not channels and not query_assets:
            # 只允许真实文字匹配或显式浏览，不把读图失败降级成随机同章题。
            for q in self.store.questions(course_id):
                if allowed(q) and (not semantic_query.strip() or semantic_query.strip() in q.stem):
                    assets = bind_assets(self.store, self.reader, q)
                    key = duplicate_key(public_snapshot(q), assets)
                    if key not in excluded_keys and key not in seen_keys:
                        seen_keys.add(key)
                        items.append(self._item(q, retrieval_text(public_snapshot(q)), assets, {"database_text": len(items) + 1}, 0, "candidate"))
            if items:
                verified["database_text"] = []
        rerank_url = getattr(self.settings, "exercise_rerank_url", "")
        if (pipeline is None and rerank_enabled and rerank_url and items
                and mode == "hybrid" and (rerank_query.strip() or query_assets)):
            t = time.perf_counter()
            try:
                shortlist = max(limit, min(getattr(self.settings, "exercise_qwen_rerank_limit", 20), 30))
                items, usage["rerank"] = await rerank_exercises(
                    rerank_url, self.vision, rerank_query, query_assets, items, intent, shortlist)
            except Exception as exc:
                errors.append("rerank:" + type(exc).__name__)
            timings["rerank_ms"] = round((time.perf_counter() - t) * 1000, 2)
        elif rerank_enabled and items and (pipeline == "D" or not rerank_url):
            t = time.perf_counter()
            try:
                subset = items[:max(1, min(self.settings.exercise_rerank_limit, 12))]
                matches, usage["rerank"] = await self.vision.rerank(semantic_query, query_assets, subset, intent)
                # 未确认差异不自动否决；模型排序只调整有限候选，不能伪装条件证明。
                subset.sort(key=lambda i: (-matches[i["id"]]["relevance"], -i["rrf_score"]))
                for item in subset:
                    match = matches[item["id"]]
                    item.update(rerank_score=match["relevance"], match_kind=match["match_kind"], match_evidence=match["evidence"],
                        differences=match["differences"], unknowns=match["unknowns"], rerank_model=self.settings.llm_model)
                items = subset + items[len(subset):]
            except Exception as exc:
                errors.append("rerank:" + type(exc).__name__)
            timings["rerank_ms"] = round((time.perf_counter() - t) * 1000, 2)
        return self._finish(items[:limit], course_id, session_id, intent, verified, timings, usage, errors, start, extraction_status,
            {"pipeline": pipeline, "query_hash": digest([query, source_question_id, asset_ids]), "candidate_count": candidate_count,
             "projection_mode": "enhanced" if projections else "baseline", "namespace": self.namespace,
             "generations": sorted({p.generation for p in projections.values()}),
             "indices": [text_index, visual_index if visual_enabled else None], "rankings": [{k: v for k, v in r.items() if k != "source"} for r in ranked]})

    @staticmethod
    def _item(q, text, assets, ranks, score, match_kind):
        question = public_question(q, assets=assets)
        return {"id": q.id, "title": public_snapshot(q)["stem"][:100], "text": text, "question": question,
            "course_id": q.course_id, "concept_ids": q.concept_ids, "source_ref": q.source_ref, "origin": (q.validation or {}).get("origin", "dataset"),
            "resource_version": {"question_version": q.version, "source_hash": source_hash(public_snapshot(q), assets)},
            "score": score, "rrf_score": score, "channels": list(ranks), "ranks": ranks, "match_kind": match_kind,
            "assets": [{"id": a["id"], "role": a["role"], "status": a["status"], "url": a["source"].get("url", "/api/assets/exercises/" + a["id"])} for a in assets],
            "_source_hash": source_hash(public_snapshot(q), assets), "_assets": assets}

    def _finish(self, items, course_id, session_id, intent, channels, timings, usage, errors, start, extraction_status, extra=None):
        current_items = []
        for item in items:
            try:
                current = self.store.question(item["id"], course_id)
                if source_hash(public_snapshot(current), bind_assets(self.store, self.reader, current)) != item["_source_hash"]:
                    raise ValueError("检索期间来源已变化")
                current_items.append(item)
                if any(a["status"] != "ready" for a in item["assets"]):
                    errors.append("candidate_asset_missing:" + item["id"])
            except ValueError:
                errors.append("source_changed_before_return:" + item["id"])
            item.pop("_assets", None)
            item.pop("_source_hash", None)
        items = current_items
        timings["total_ms"] = round((time.perf_counter() - start) * 1000, 2)
        receipt = {"intent": intent, "channels_used": list(channels), "degraded": bool(errors), "reason": "; ".join(dict.fromkeys(errors)),
            "timings": timings, "usage": usage, "extraction_status": extraction_status, "returned_ids": [i["id"] for i in items], **(extra or {})}
        identifier = self.store.receipt(course_id, session_id, receipt)
        return {"items": items, "citations": [{"id": i["id"], "source_ref": i["source_ref"], "title": i["title"]} for i in items], **receipt, "receipt_id": identifier, "trace_id": identifier}
