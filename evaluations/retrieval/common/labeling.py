"""盲化的银标判断与复判，裁判不会看到实验来源、排名或分数。"""

import random
import math
from pathlib import Path
from .ai import Clients
from .files import read_rows, read_json, write_rows, write_json, mutable, digest
from .config import SEED
from .annotation import judge_batches

LABEL_PROMPT = """你是数学检索相关性标注员。只评价给定候选是否满足查询意图，不评价检索系统。
输出JSON：{"judgments":[{"candidate":"c0","grade":0,"uncertain":false,"near_miss":false,"reason":"依据与关键差异"}]}。
对每个候选独立判断，必须覆盖所有临时候选编号，每个编号恰好一次；候选之间不作排序。
grade取0至3：3高度满足目标；2是有效相关例题但有可接受差异；1仅主题/知识点相关而不满足目标；0无关。
exact必须题面数值、条件、选项和所求一致（措辞变化允许），匹配给3，否则最多1。
isomorphic必须数学关系与操作结构一致；same_method必须能够示范同一方法及其适用条件。
图片外观相似或知识点相同都不足以证明同构。参考解析仅用于核对数学方法，不是检索输入。
输入text包含冻结的图示转录，未提供原始像素；仅依据明确的转录事实和参考解析判断。
转录未记载的图形标记不可猜测，缺少判断所需的关键证据时返回null，不因没有像素本身而否定完整文字证据。
看不清图或证据不足时grade为null且uncertain=true，不编造条件。困难近似负例near_miss=true。
reason只写判断依据，不提供完整答案或解题过程。"""


def validate_judgment(value):
    grade = value.get("grade")
    if grade is not None and (type(grade) is not int or grade not in range(4)):
        raise ValueError("相关性等级必须是0至3或null")
    if not isinstance(value.get("uncertain"), bool) or not isinstance(
        value.get("reason"), str
    ):
        raise ValueError("相关性回执缺少不确定状态或依据")
    if not value["reason"].strip() or not isinstance(
        value.get("near_miss", False), bool
    ):
        raise ValueError("判断依据不能为空，困难负例标记必须为布尔值")
    if value["uncertain"]:
        value["grade"] = None
    return value


async def label_pool(dataset, *, limit=None):
    dataset = Path(dataset)
    mutable(dataset)
    corpus = {r["id"]: r for r in read_rows(dataset / "corpus.jsonl")}
    queries = {q["id"]: q for q in read_rows(dataset / "queries.jsonl")}
    references = read_json(dataset / "private/references.json", {})
    existing = {
        (r["query_id"], r["question_id"]): r for r in read_rows(dataset / "qrels.jsonl")
    }
    clients = Clients(dataset)
    processed = 0
    try:
        for pool in read_rows(dataset / "pools.jsonl"):
            q = queries[pool["query_id"]]
            if all(
                existing.get((q["id"], cid), {}).get("review_complete")
                for cid in pool["candidate_ids"]
            ):
                continue
            pending = [
                cid
                for cid in pool["candidate_ids"]
                if not existing.get((q["id"], cid), {}).get("first")
            ]
            if limit:
                pending = pending[: max(0, limit - processed)]

            def save(stage):
                def callback(judgments):
                    for cid, judgment in judgments.items():
                        row = existing.setdefault(
                            (q["id"], cid),
                            {
                                "query_id": q["id"],
                                "question_id": cid,
                                "label_kind": "silver",
                                "human_reviewed": False,
                                "judge_input": "frozen_text_and_private_reference",
                                "review_complete": False,
                                "grade": None,
                                "near_miss": False,
                                "reason": "待复判",
                                "second": None,
                                "adjudication": None,
                            },
                        )
                        row[stage] = judgment
                    write_rows(dataset / "qrels.jsonl", existing.values())

                return callback

            await judge_batches(
                clients,
                q,
                [corpus[cid] for cid in pending],
                references,
                "label_first",
                LABEL_PROMPT,
                validate_judgment,
                save("first"),
            )
            processed += len(pending)
            # 未完成该查询的初判前不抽负例，恢复执行时复核抽样集合保持稳定。
            if any(
                not existing.get((q["id"], cid), {}).get("first")
                for cid in pool["candidate_ids"]
            ):
                return {
                    "pairs_completed": sum(
                        r.get("review_complete", False) for r in existing.values()
                    ),
                    "partial": True,
                }
            rows = [existing[(q["id"], cid)] for cid in pool["candidate_ids"]]
            review_ids = {
                r["question_id"]
                for r in rows
                if r["first"]["grade"] is None or r["first"]["grade"] >= 2
            }
            for grade in (0, 1):
                negatives = sorted(
                    r["question_id"] for r in rows if r["first"]["grade"] == grade
                )
                rng = random.Random(SEED + int(digest([q["id"], grade])[:8], 16))
                review_ids.update(
                    rng.sample(negatives, math.ceil(len(negatives) * 0.2))
                )
            second_rows = [
                corpus[r["question_id"]]
                for r in rows
                if r["question_id"] in review_ids and not r.get("second")
            ]
            await judge_batches(
                clients,
                q,
                second_rows,
                references,
                "label_second",
                LABEL_PROMPT
                + "\n这是独立复核请求，重点检查条件遗漏、图示误读及方法适用性。",
                validate_judgment,
                save("second"),
            )
            disputes = [
                r
                for r in rows
                if r.get("second")
                and r["first"]["grade"] != r["second"]["grade"]
                and not r.get("adjudication")
            ]
            await judge_batches(
                clients,
                q,
                [corpus[r["question_id"]] for r in disputes],
                references,
                "label_adjudicate",
                LABEL_PROMPT
                + "\n前两次判断有分歧，重新核实原始证据；仍不能确认必须返回null。",
                validate_judgment,
                save("adjudication"),
                {r["question_id"]: [r["first"], r["second"]] for r in disputes},
            )
            for row in rows:
                final = row.get("adjudication") or row.get("second") or row["first"]
                row.update(
                    grade=final["grade"],
                    reason=final["reason"],
                    review_complete=True,
                    near_miss=bool(final.get("near_miss")) and final["grade"] in (0, 1),
                )
            write_rows(dataset / "qrels.jsonl", existing.values())
            print(
                f"labeled {q['id']} pairs={len(rows)} reviewed={len(review_ids)}",
                flush=True,
            )
            if limit and processed >= limit:
                return {
                    "pairs_completed": sum(
                        r.get("review_complete", False) for r in existing.values()
                    ),
                    "partial": True,
                }
        deficits = []
        for q in queries.values():
            labels = [r for (query_id, _), r in existing.items() if query_id == q["id"]]
            if not any(r["grade"] is not None and r["grade"] >= 2 for r in labels):
                deficits.append(
                    {
                        "query_id": q["id"],
                        "source_group": q["source_group"],
                        "reason": "no_confirmed_positive",
                        "has_unknown": any(r["grade"] is None for r in labels),
                    }
                )
        write_json(dataset / "review/replacement_requests.json", deficits)
        return {
            "pairs_completed": len(existing),
            "queries_without_positive": len(deficits),
            "unjudged": sum(r["grade"] is None for r in existing.values()),
        }
    finally:
        await clients.close()
