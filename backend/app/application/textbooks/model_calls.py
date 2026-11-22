"""真实调用前落回执，结构验证后才缓存；保留失败调用与实际用量。"""

import json
import time
from pydantic import ValidationError
from app.infrastructure.documents.pdf import digest


async def structured_call(model, store, job_id, stage, page, messages, contract, max_tokens, _repair=0):
    request_hash = digest([model.settings.llm_model, messages, max_tokens])
    cached = store.cached_call(job_id, request_hash)
    if cached:
        if cached["cache_source_job"] != job_id:
            identifier = store.call_started(job_id, stage, page, model.settings.llm_model, request_hash)
            store.call_finished(identifier, response=cached, metrics={"cache_hit": True, "source_call_id": cached["cache_source_id"], "usage": {}})
        return contract.model_validate(cached["validated"]).model_dump()
    if stage == "page_review" and store.truncated_call(request_hash):
        raise ValueError("相同审校请求已有截断回执，保留原转录等待原页复核，不盲目重发。")
    identifier = store.call_started(job_id, stage, page, model.settings.llm_model, request_hash)
    start = time.perf_counter()
    response = None
    try:
        response = await model.chat(messages, json_output=True, max_tokens=max_tokens)
        choice = response["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ValueError("模型输出被截断，不能作为完整提取结果发布。")
        value = json.loads(choice["message"]["content"])
        # 部分兼容接口会回显格式元数据；只移除此精确传输标记，不宽松吞掉业务字段。
        if value.get("type") == "json_object":
            value.pop("type")
        validated = contract.model_validate(value).model_dump()
        metrics = {"duration_ms": round((time.perf_counter() - start) * 1000, 2),
                   "usage": response.get("usage", {}), "request_metrics": response.get("_request_metrics", {}),
                   "context_metrics": response.get("_context_metrics", {})}
        store.call_finished(identifier, response={"raw": response, "validated": validated}, metrics=metrics)
        return validated
    except BaseException as exc:
        # 不保存异常响应文本，避免服务商错误意外带出鉴权信息。
        store.call_finished(identifier, response={"raw": response} if response else {},
            metrics={"duration_ms": round((time.perf_counter() - start) * 1000, 2), "usage": (response or {}).get("usage", {}),
                     "request_metrics": (response or {}).get("_request_metrics", {})},
            error=type(exc).__name__ + ": " + str(exc)[:500] if isinstance(exc, (ValueError, KeyError)) else type(exc).__name__, status="failed")
        if _repair < 1 and isinstance(exc, (ValidationError, json.JSONDecodeError)) and response:
            raw = response["choices"][0]["message"]["content"]
            problem = json.dumps(exc.errors(include_input=False), ensure_ascii=False) if isinstance(exc, ValidationError) else "JSON格式无效"
            repair_messages = [*messages, {"role": "assistant", "content": raw},
                {"role": "user", "content": "仅修复以下输出结构问题，保留原页内容，不求解或新增条件。返回完整JSON：" + problem}]
            fixed = await structured_call(model, store, job_id, stage + "_repair", page, repair_messages, contract, max_tokens, _repair + 1)
            repair_hash = digest([model.settings.llm_model, repair_messages, max_tokens])
            repaired_receipt = store.cached_call(job_id, repair_hash)
            alias = store.call_started(job_id, stage, page, model.settings.llm_model, request_hash)
            store.call_finished(alias, response={"validated": fixed, "repair_source_call_id": repaired_receipt["cache_source_id"]},
                metrics={"cache_hit": True, "source_call_id": repaired_receipt["cache_source_id"], "usage": {}})
            return fixed
        raise
