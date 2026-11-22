"""只使用公开题面读图，保存真实响应与请求用量。"""

import json
import time
from app.domains.courses.questions.search.schemas import VisualExtraction, RerankOutput
from app.domains.courses.questions.search.representation import digest, validate_extraction
from app.domains.courses.questions.images import require_public_asset
from app.infrastructure.documents.question_images import ImageBudget

PROMPT_VERSION = "exercise-vision-v2"
EXTRACTION_PROMPT = """你是教材题面转录员。只转录公开题干和选项，不解题，不补充答案或推导结论。
从输入的真实图像提取OCR、公式、明确条件、所求和小问。不得从外形猜垂直、平行、等长。
每个事实带evidence：文字明示为explicit_text，quote必须逐字来自提供的题干/选项，asset_id和region为null；
图像来源提供真实asset_id、[x1,y1,x2,y2]的0到1归一化region，标记为explicit_diagram_marker或readable_value。
看不清的内容放unknowns，禁止编造数字、符号和条件。image_roles允许多标签，装饰也必须按实际作用判断。
只输出符合给定JSON Schema的JSON对象，模型无权填写review_status。"""


class QuestionVision:
    def __init__(self, models, store, reader):
        self.models, self.store, self.reader = models, store, reader

    async def structured(self, stage, payload, images, schema, *, cache=True, validator=None):
        key = digest([PROMPT_VERSION, stage, self.models.settings.llm_model, payload, [[i["hash"], i.get("label")] for i in images], schema.model_json_schema()])
        old = self.store.cached_call(key) if cache else None
        def decode(response):
            if response["choices"][0].get("finish_reason") == "length":
                raise ValueError("模型输出被截断，需检查任务预算")
            value = schema.model_validate_json(response["choices"][0]["message"]["content"]).model_dump()
            if validator:
                validator(value)
            return value
        if old:
            try:
                return decode(old.response), {"cache_hit": True, "requests": 0}
            except (ValueError, KeyError):
                pass
        instructions = EXTRACTION_PROMPT if stage == "extract" else """按intent判断数学任务相关性，不求答案、不引用私有解析。题图相似不能代替条件一致。
exact检查数值、条件与所求；isomorphic检查关系和操作结构；same_method检查可示范的方法，不要求数值和条件全同。
看不清标记放unknowns，不能认定不存在。只返回提供的question_id，每个候选一次；保留差异和依据，禁止补造来源。
relevance为0到3的排序等级，不是正确率。只输出符合Schema的JSON。"""
        parts = [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]
        for image in images:
            parts.extend([{"type": "text", "text": image["label"]}, {"type": "image_url", "image_url": {"url": image["url"], "detail": "high"}}])
        messages = [{"role": "system", "content": instructions + "\nJSON Schema:\n" + json.dumps(schema.model_json_schema(), ensure_ascii=False)}, {"role": "user", "content": parts}]
        attempts = []
        for attempt in range(2):
            start = time.perf_counter()
            response = {}
            try:
                response = await self.models.chat(messages, json_output=True, max_tokens=6500 if stage == "extract" else 3000)
                value = decode(response)
                metrics = {"elapsed_ms": round((time.perf_counter() - start) * 1000, 2), "usage": response.get("usage", {}),
                           "request_metrics": response.get("_request_metrics", {}), "model": self.models.settings.llm_model,
                           "cache_hit": False, "repair_attempt": attempt, "previous_attempts": attempts}
                self.store.call(key, stage, response, metrics, "completed")
                return value, metrics
            except Exception as exc:
                failure = {"model": self.models.settings.llm_model, "elapsed_ms": round((time.perf_counter() - start) * 1000, 2), "error": type(exc).__name__,
                    "usage": response.get("usage", {}), "request_metrics": response.get("_request_metrics", {})}
                self.store.call(key, stage, response, failure, "failed")
                attempts.append(failure)
                if attempt or not isinstance(exc, ValueError) or not response:
                    raise
                # 最多一次结构/来源修复，不把模型置信度当作人工复核。
                messages.extend([{"role": "assistant", "content": response.get("choices", [{}])[0].get("message", {}).get("content", "")},
                    {"role": "user", "content": "修复JSON和来源错误：" + str(exc)[:1500] + "。explicit_text的quote必须逐字出现在原题或选项中，包括$$等原始符号；不要改写证据。"}])

    def pixels(self, assets, label="题图"):
        if len(assets) > 10:
            raise ValueError("题图数量超过本任务预算")
        result = []
        budget = ImageBudget(10, 12 * 1024 * 1024)
        for a in assets:
            require_public_asset(a)
            image = self.reader.read(a["location"], a["content_hash"])
            budget.add(image)
            role_label = ",".join(a["source"].get("fields", [a["source"].get("field", a["role"])]))
            result.append({**image, "role_label": role_label, "label": f"{label} {role_label} asset_id={a['id']}"})
        return result

    async def extract(self, snapshot, assets):
        payload = {"stem": snapshot["stem"], "options": snapshot["options"], "asset_ids": [a["id"] for a in assets]}
        value, metrics = await self.structured("extract", payload, self.pixels(assets), VisualExtraction,
            validator=lambda value: validate_extraction(value, assets, snapshot))
        return value, metrics

    async def rerank(self, query, query_assets, candidates, intent):
        from app.shared.llm_budget import assert_request_budget, ContextBudgetExceeded
        # 每批保留完整查询与完整候选，通过多批比较适配上下文，不截断条件或丢弃题图。
        base_images = self.pixels(query_assets, "查询题")
        batches, current, images = [], [], list(base_images)
        def payload(items):
            return {"intent": intent, "query": query, "candidates": [{"question_id": i["id"], "text": i["text"],
                "assets": [a["id"] for a in i["_assets"]]} for i in items]}
        def fits(items, pictures):
            if len(pictures) > 16 or sum(i["bytes"] for i in pictures) > 12 * 1024 * 1024:
                return False
            # 为实际系统提示保留额外文字余量；共享模型出口仍执行最终完整检查。
            messages = [{"role": "system", "content": "按数学意图重排，不解题。" + json.dumps(RerankOutput.model_json_schema()) + "x" * 1400},
                {"role": "user", "content": [{"type": "text", "text": json.dumps(payload(items), ensure_ascii=False)},
                    *[{"type": "image_url", "image_url": {"url": i["url"]}} for i in pictures]]}]
            try:
                assert_request_budget(self.models.settings, messages, max_tokens=3000, json_output=True)
                return True
            except ContextBudgetExceeded:
                return False
        for item in candidates:
            added = self.pixels(item["_assets"], item["id"])
            if not fits(current + [item], images + added):
                if not current:
                    raise ValueError("完整单题重排超出预算")
                batches.append((current, images))
                current, images = [], list(base_images)
                if not fits([item], images + added):
                    raise ValueError("完整单题重排超出预算")
            current.append(item)
            images.extend(added)
        if current:
            batches.append((current, images))
        result, receipts = {}, []
        for items, pictures in batches:
            def validate_matches(value):
                ids = [v["question_id"] for v in value["matches"]]
                if len(ids) != len(set(ids)) or set(ids) != {c["id"] for c in items}:
                    raise ValueError("重排结果必须逐一覆盖提供的候选题目，不能新增、遗漏或重复题号")
            value, metrics = await self.structured("rerank", payload(items), pictures, RerankOutput, cache=False, validator=validate_matches)
            result.update({v["question_id"]: v for v in value["matches"]})
            receipts.append(metrics)
        return result, {"batches": receipts, "candidate_count": len(candidates), "score_notice": "分批相关性等级仅用于排序，未证明跨批分值校准"}
