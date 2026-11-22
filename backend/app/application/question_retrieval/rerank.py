"""为习题精排保留向量召回名额，并用关键词召回补充候选。"""

from app.infrastructure.clients.reranker import LocalReranker
from app.infrastructure.documents.question_images import ImageBudget


def select_candidates(items, limit):
    def channel(name):
        return sorted((item for item in items if name in item["ranks"]), key=lambda item: item["ranks"][name])

    vector = channel("text_vector")
    head = (3 * limit + 4) // 5
    selected = {}
    for item in vector[:head] + channel("keyword") + vector[head:] + items:
        selected.setdefault(item["id"], item)
        if len(selected) == limit:
            break
    return list(selected.values())


async def rerank_exercises(url, vision, query, query_assets, items, intent, limit):
    subset = select_candidates(items, limit)
    budget = ImageBudget(64, 12 * 1024 * 1024)

    def row(text, assets):
        images = []
        for image in vision.pixels(assets):
            budget.add(image)
            images.append(image["url"])
        return {"text": text, "images": images}

    scores, metrics = await LocalReranker(url).score(
        row(query, query_assets), [{"id": item["id"], **row(item["text"], item["_assets"])} for item in subset], intent)
    # 模型只给排序分；相同分数沿用融合排名，未打分的候选保持原有顺序。
    ordered = sorted(subset, key=lambda item: (-scores[item["id"]], -item["rrf_score"]))
    return ([{**item, "rerank_score": scores[item["id"]], "rerank_model": metrics["model"]} for item in ordered]
            + [item for item in items if item["id"] not in scores]), metrics
