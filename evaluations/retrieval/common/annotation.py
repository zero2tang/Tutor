"""按完整上下文预算组批标注，批内使用临时编号隐藏题库标识。"""

import asyncio
from app.shared.llm_budget import assert_request_budget, ContextBudgetExceeded
from app.domains.courses.questions.search.representation import clean_text


def reference_text(value):
    return {
        key: clean_text(str(value.get(key, ""))) for key in ("answer", "explanation")
    }


def annotation_payload(query, rows, references, prior=None):
    # 银标基于完整公开转录与隔离解析；原图保留在人工复核包，明确记录这一证据边界。
    assets = []
    candidates = []
    for i, row in enumerate(rows):
        item = {
            "candidate": f"c{i}",
            "text": row["text"],
            "has_image": bool(row["assets"]),
            "diagram_evidence": "frozen_transcription_in_text",
            "private_reference": reference_text(references.get(row["id"], {})),
        }
        if prior is not None:
            item["prior_judgments"] = prior[row["id"]]
        candidates.append(item)
    return {
        "intent": query["intent"],
        "query": query["text"],
        "query_has_image": bool(query["assets"]),
        "private_query_reference": reference_text(
            references.get(query["source_id"], {})
        ),
        "candidates": candidates,
    }, assets


def annotation_batches(clients, query, rows, references, instructions, prior=None):
    batches, current = [], []

    def fits(items):
        payload, assets = annotation_payload(query, items, references, prior)
        if len(assets) > 10:
            return False
        try:
            assert_request_budget(
                clients.models.settings,
                clients.messages(payload, assets, instructions),
                max_tokens=3000,
                json_output=True,
            )
            return True
        except ContextBudgetExceeded:
            return False

    for row in rows:
        if current and (len(current) >= 8 or not fits([*current, row])):
            batches.append(current)
            current = []
        if not fits([row]):
            raise ValueError("完整单对标注超出预算，未截断题面或解析：" + row["id"])
        current.append(row)
    if current:
        batches.append(current)
    return batches


async def judge_batches(
    clients, query, rows, references, stage, instructions, validate, save, prior=None
):
    batches = annotation_batches(clients, query, rows, references, instructions, prior)
    semaphore = asyncio.Semaphore(3)

    async def judge(batch):
        async with semaphore:
            payload, assets = annotation_payload(query, batch, references, prior)

            def check(value):
                judgments = value.get("judgments", [])
                aliases = [item.get("candidate") for item in judgments]
                expected = {f"c{i}" for i in range(len(batch))}
                if len(aliases) != len(set(aliases)) or set(aliases) != expected:
                    raise ValueError(
                        "标注必须逐一覆盖临时候选编号，不得遗漏、重复或增加"
                    )
                for item in judgments:
                    validate(item)

            value = await clients.json_call(
                stage, payload, assets, instructions, validator=check
            )
            judgments = value["judgments"]
            by_alias = {item["candidate"]: validate(item) for item in judgments}
            save({row["id"]: by_alias[f"c{i}"] for i, row in enumerate(batch)})

    # 同一事件循环内，保存回调不含await，原子写盘与内存记录保持一致。
    results = await asyncio.gather(
        *(judge(batch) for batch in batches), return_exceptions=True
    )
    for result in results:
        if isinstance(result, Exception):
            raise result
