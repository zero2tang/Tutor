"""可信本地构建命令；审阅文件是操作者输入，不由模型自行批准。"""

import argparse
import asyncio
import json
from pathlib import Path
from app.application.model_registry import register_models
from app.infrastructure.persistence.database import create_tables
from app.infrastructure.clients.llm import ModelClient
from app.infrastructure.clients.search import SearchClient
from app.infrastructure.clients.question_index import QuestionIndex
from .service import QuestionRetrievalService
from .build import QuestionSearchBuilder
from .assets import bind_assets, register_textbook_crop


async def run(args):
    create_tables(register_models())
    models, search = ModelClient(), SearchClient()
    service = QuestionRetrievalService(models, search, namespace=args.namespace)
    builder = QuestionSearchBuilder(service.store, service.reader, service.vision, models, search, service.multimodal, args.namespace)
    try:
        ids = args.question_ids.split(",") if args.question_ids else None
        if args.action == "inventory":
            rows = []
            for q in service.store.questions(args.course):
                if ids and q.id not in ids:
                    continue
                snapshot, assets, fingerprint = builder.current(q)
                reviewed = service.store.selected_representation(q.id, fingerprint)
                rows.append({"question_id": q.id, "stem": q.stem, "options": q.options, "source_ref": q.source_ref,
                    "source_hash": fingerprint, "assets": assets,
                    "image_role": reviewed.payload["image_roles"] if reviewed else "pending" if assets else {},
                    "review_status": "approved" if reviewed else "pending" if assets else "native_only",
                    "intent_labels": "见独立冻结评测集"})
            result = {"course_id": args.course, "questions": len(rows), "with_images": sum(bool(r["assets"]) for r in rows), "items": rows}
        elif args.action == "extract":
            if not ids:
                raise ValueError("提取必须明确题目列表，避免无界模型调用")
            results = []
            for qid in ids:
                rep = await builder.extract(qid, args.course)
                results.append({"representation_id": rep.id, "question_id": qid, "source_hash": rep.source_hash,
                    "payload": rep.payload, "review_status": rep.review_status, "asset_ids": rep.asset_ids})
            result = {"items": results}
        elif args.action == "review":
            amendments = json.loads(args.input.read_text(encoding="utf-8"))
            for item in amendments["items"]:
                rep = service.store.representation(item["representation_id"])
                if not rep or rep.course_id != args.course or builder.current(service.store.question(rep.question_id, args.course))[2] != item["source_hash"]:
                    raise ValueError("原图、题面或课程已变化，不能复核旧版本")
            result = {"reviewed_ids": [service.store.review(i["representation_id"], i["payload"], reviewer=i["reviewer"],
                notes=i["notes"], expected_hash=i["source_hash"]) for i in amendments["items"]]}
        elif args.action == "build":
            result = await builder.build(args.course, ids, visual=args.visual)
        elif args.action == "withdraw":
            if not ids:
                raise ValueError("撤回必须明确题目列表")
            result = []
            for qid in ids:
                service.store.withdraw(qid, args.course)
                result.append({"question_id": qid, "index_errors": await QuestionIndex(search).withdraw(args.course, qid)})
        elif args.action == "crop":
            value = json.loads(args.input.read_text(encoding="utf-8"))
            q = service.store.question(value["question_id"], args.course)
            result = {"asset_id": register_textbook_crop(service.store, service.reader, q, value["path"], value["source"], value["reviewer"])}
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        # 清单与模型转录写到文件，控制台只报告规模和状态。
        print(json.dumps({"action": args.action, "output": str(args.output) if args.output else None,
            "result": result if args.action in {"build", "withdraw", "crop"} else {"count": len(result.get("items", result.get("reviewed_ids", [])))}}, ensure_ascii=False))
    finally:
        await asyncio.gather(models.close(), search.close())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inventory", "extract", "review", "build", "withdraw", "crop"])
    parser.add_argument("--course", required=True)
    parser.add_argument("--question-ids")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--visual", action="store_true")
    parser.add_argument("--namespace", default="main")
    args = parser.parse_args()
    if args.action in {"review", "crop"} and args.input is None:
        parser.error("此操作需要 --input 审阅文件")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
