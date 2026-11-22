"""离线工作进程：真实调用配置的多模态模型，按节恢复，范围显式受限。"""

import argparse
import asyncio
import json
from pathlib import Path
from app.application.model_registry import register_models
from app.infrastructure.persistence.database import engine
from .extraction import TextbookExtraction


async def main():
    parser = argparse.ArgumentParser(description="教材三类资源自动提取；先复核1.1，再处理第一章")
    parser.add_argument("--source", required=True)
    parser.add_argument("--course", default="junior_math_7a")
    parser.add_argument("--section")
    parser.add_argument("--chapter", type=int, choices=[1], default=1)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--review", help="对应当前小节内容指纹的本地复核JSON")
    parser.add_argument("--amend", help="原页更正JSON，包含source_section_id、patches和reviewer；生成新快照")
    args = parser.parse_args()
    register_models().create_all(engine())
    worker = TextbookExtraction()
    try:
        book_id, manifest = await asyncio.to_thread(worker.prepare, args.source, course_id=args.course, chapter=args.chapter)
        print(json.dumps({"book_id": book_id, "manifest": manifest}, ensure_ascii=False), flush=True)
        if args.prepare_only:
            return
        if args.amend:
            correction = json.loads(Path(args.amend).read_text(encoding="utf-8"))
            source = worker.store.section(correction["source_section_id"])
            if source.book_id != book_id:
                raise ValueError("更正快照不属于当前教材。")
            identifier = worker.store.amend(source.id, correction["patches"], correction["reviewer"])
            worker.export(identifier)
            print(json.dumps({"amended_section_id": identifier}, ensure_ascii=False))
            return
        selected = [s for s in manifest["sections"] if s["number"] == args.section] if args.section else manifest["sections"]
        if not selected:
            raise ValueError("未找到指定小节。")
        if args.review:
            if len(selected) != 1:
                raise ValueError("一次只能复核一个小节。")
            review = json.loads(Path(args.review).read_text(encoding="utf-8"))
            identifier = review.get("section_id") or worker.store.create_job(book_id, selected[0], worker.fingerprint())
            row = worker.store.section(identifier)
            if row.book_id != book_id or row.number != selected[0]["number"] or worker.store.job(identifier).fingerprint != worker.fingerprint():
                raise ValueError("复核回执不属于当前教材、小节或提取版本。")
            worker.store.review(identifier, review)
            worker.export(identifier)
            return
        if args.section != "1.1":
            worker.require_pilot_review(book_id, manifest)
        for section in selected:
            if not args.section and section["number"] == "1.1":
                worker.export(worker.require_pilot_review(book_id, manifest).id)
                continue
            await worker.extract(book_id, section)
    finally:
        await worker.close()


if __name__ == "__main__":
    asyncio.run(main())
