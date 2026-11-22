"""固定试点题库，真实重放四组检索；报告保留每道题、原图、排名和用量。"""

import argparse
import asyncio
import html
import json
from pathlib import Path
import sys
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from app.application.model_registry import register_models
from app.infrastructure.persistence.database import create_tables
from app.infrastructure.clients.llm import ModelClient
from app.infrastructure.clients.search import SearchClient
from app.application.question_retrieval.service import QuestionRetrievalService
from exercise_retrieval_metrics import query_metrics, summarize, usage_totals


def validate_dataset(dataset):
    if not dataset.get("reviewer") or not dataset.get("frozen_before_evaluation"):
        raise ValueError("评测标签必须先核对和冻结")
    seen, source_splits = set(), {}
    for case in dataset["queries"]:
        if case["id"] in seen or case["intent"] not in {"exact", "isomorphic", "same_method"}:
            raise ValueError("查询编号或意图无效")
        seen.add(case["id"])
        if not set(case["labels"]).issubset(dataset["question_ids"]) or not any(g >= 2 for g in case["labels"].values()):
            raise ValueError("标签超出固定题库或缺少相关题")
        previous = source_splits.setdefault(case["source_group"], case["split"])
        if previous != case["split"]:
            raise ValueError("同来源变体不能跨数据划分")


def render_report(report, inventory, path):
    body = ["<!doctype html><html lang='zh'><meta charset='utf-8'><title>含图检索逐题对照</title><style>body{max-width:1250px;margin:auto;padding:24px;font:16px system-ui}img{max-width:360px;max-height:250px;background:white}article{padding:18px;border:1px solid #bbb;margin:18px 0}pre{white-space:pre-wrap;word-break:break-word}table{width:100%;border-collapse:collapse}td,th{border:1px solid #ddd;padding:8px}</style><h1>含图检索逐题对照</h1>"]
    body.append("<p>小样本工程试点，标签由原图核对，不表示最终准确率或教学收益。公开题面不含答案；全部原始回执另存JSON。</p>")
    body.append("<pre>" + html.escape(json.dumps(report["summary"], ensure_ascii=False, indent=2)) + "</pre>")
    for qid, row in inventory.items():
        body.append("<article><h2>" + html.escape(qid) + "</h2><p>" + html.escape(row["stem"]) + "</p>")
        for asset in row["assets"]:
            # 本地审阅报告保留绝对资产路径，不能用于公共网络服务。
            body.append('<img src="' + Path(asset["location"]).resolve().as_uri() + '">')
        body.append("</article>")
    for case in report["dataset"]["queries"]:
        body.append("<article><h2>" + html.escape(case["id"] + " / " + case["intent"]) + "</h2><p>" + html.escape(case.get("query_text", "")) + "</p><p>" + html.escape(case["rationale"]) + "</p><table><tr><th>组别</th><th>排名与匹配</th><th>指标与降级</th></tr>")
        for row in report["runs"]:
            if row["query_id"] != case["id"]:
                continue
            result = row["result"]
            ranks = [{"id": i["id"], "match": i["match_kind"], "channels": i["channels"], "differences": i.get("differences", [])} for i in result["items"]]
            body.append("<tr><td>" + row["group"] + "</td><td><pre>" + html.escape(json.dumps(ranks, ensure_ascii=False)) + "</pre></td><td><pre>" + html.escape(json.dumps({**row["metrics"], "reason": result["reason"]}, ensure_ascii=False)) + "</pre></td></tr>")
        body.append("</table></article>")
    path.write_text("\n".join(body) + "</html>", encoding="utf-8")


async def run(args):
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    validate_dataset(dataset)
    create_tables(register_models())
    models, search = ModelClient(), SearchClient()
    service = QuestionRetrievalService(models, search, namespace=args.namespace)
    from app.application.question_retrieval.assets import bind_assets
    inventory = {}
    for qid in dataset["question_ids"]:
        q = service.store.question(qid, dataset["course_id"])
        inventory[qid] = {"stem": q.stem, "assets": bind_assets(service.store, service.reader, q)}
    report = {"dataset": dataset, "started_at": datetime.now(timezone.utc).isoformat(), "runs": [],
        "models": {"text": models.settings.embedding_model, "visual": service.multimodal.space, "rerank": models.settings.llm_model},
        "limits": {"candidates": models.settings.exercise_candidates, "rerank": models.settings.exercise_rerank_limit, "top_k": args.top_k},
        "namespace": args.namespace, "limitations": ["真实题库的16题固定试点，标签由Codex原图核对，尚无独立教师标注", "未覆盖物理、函数图像、整题截图、跨页和所有困难负例", "本次为工程验收与开发集对照，不能作为泛化效果承诺", "未配置单价时只报告真实Tokens和请求，不估算费用"]}
    report["projections"] = [{"question_id": p.question_id, "id": p.id, "generation": p.generation,
        "source_hash": p.source_hash, "config": p.payload["config"]} for p in service.store.projections(dataset["course_id"], namespace=args.namespace)]
    try:
        for case in dataset["queries"]:
            for group in args.groups.split(","):
                start = time.perf_counter()
                result = await service.retrieve(case.get("query_text", ""), course_id=dataset["course_id"], intent=case["intent"],
                    source_question_id=case.get("source_question_id"), top_k=args.top_k, pipeline=group,
                    candidate_question_ids=dataset["question_ids"])
                ids = [i["id"] for i in result["items"]]
                metrics = query_metrics(ids, case["labels"], args.top_k)
                metrics["hard_negative_in_top_k"] = sum(qid in case.get("hard_negative_ids", []) for qid in ids)
                report["runs"].append({"query_id": case["id"], "intent": case["intent"], "input_kind": "image" if case.get("source_question_id") else "text", "group": group,
                    "metrics": metrics, "result": result, "wall_ms": round((time.perf_counter() - start) * 1000, 2)})
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({"query": case["id"], "group": group, "ids": ids, "metrics": metrics, "degraded": result["degraded"]}, ensure_ascii=False), flush=True)
        report["summary"] = summarize(report["runs"])
        report["by_intent"] = {intent: summarize([r for r in report["runs"] if r["intent"] == intent]) for intent in ("exact", "isomorphic", "same_method")}
        report["by_input"] = {kind: summarize([r for r in report["runs"] if r["input_kind"] == kind]) for kind in ("text", "image")}
        report["usage_by_group"] = {group: usage_totals([r["result"]["usage"] for r in report["runs"] if r["group"] == group]) for group in args.groups.split(",")}
        report["cache_notice"] = "逐次回执记录向量缓存与实际请求；本轮不是严格冷启动实验，重排为实际模型调用。"
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        render_report(report, inventory, args.output.with_suffix(".html"))
        print(json.dumps(report["summary"], ensure_ascii=False))
    finally:
        await asyncio.gather(models.close(), search.close())


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--namespace", default="pilot-v1")
    p.add_argument("--groups", default="A,B,C,D")
    p.add_argument("--top-k", type=int, default=5)
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
