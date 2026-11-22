"""保持既有向量调用兼容，同时向检索任务提供真实缓存和用量回执。"""


async def text_embedding(models, texts):
    tracked = getattr(models, "embed_tracked", None)
    if callable(tracked):
        return await tracked(texts)
    return await models.embed(texts), {"usage_available": False}
