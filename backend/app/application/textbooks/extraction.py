"""按小节执行真实视觉提取，分页结果可恢复，三类资源在同一事务发布。"""

import asyncio
import base64
import json
from pathlib import Path
from app.infrastructure.clients.llm import ModelClient
from app.infrastructure.config import ROOT
from app.infrastructure.documents.pdf import prepare_pdf, page_input, digest
from app.domains.materials.extraction.store import ExtractionStore
from app.domains.materials.extraction.schemas import PageExtraction, PageReview, TeachingDesign
from .prompts import PAGE_PROMPT, DESIGN_PROMPT, AUDIT_PROMPT, VERSION
from .model_calls import structured_call
from .assembly import attach_page, assemble, design_input, validate_design


class TextbookExtraction:
    def __init__(self, model=None, store=None):
        self.model = model or ModelClient()
        self.store = store or ExtractionStore()
        self.limit = asyncio.Semaphore(2)

    def prepare(self, source, *, course_id="junior_math_7a", title="人教版数学七年级上册", chapter=1):
        manifest, root, raw = prepare_pdf(Path(source), ROOT / "data/raw/textbook_extraction", chapter)
        book_id = self.store.register(manifest, root, raw, course_id, title)
        return book_id, manifest

    def fingerprint(self):
        return digest([VERSION, PAGE_PROMPT, DESIGN_PROMPT, AUDIT_PROMPT, self.model.settings.llm_model, "poppler145-jpeg92"])

    async def extract(self, book_id, section):
        job_id = self.store.create_job(book_id, section, self.fingerprint())
        token = self.store.claim(job_id)
        if token is None:
            return self.store.section(job_id)
        root = Path(self.store.book(book_id).asset_root)
        try:
            async def one(number):
                async with self.limit:
                    info = await asyncio.to_thread(page_input, root, number)
                    previous = root / f"page-{number - 1:03}.json"
                    context = json.loads(previous.read_text(encoding="utf-8"))["text"][-1000:] if previous.exists() else ""
                    y0 = section.get("start_y", 0) if number == section["page_start"] else 0
                    y1 = section.get("end_y", 1000) if number == section["page_end"] else 1000
                    text = f"当前PDF第{number}页；本节{section['number']} {section['title']}。只提取本页归一化纵坐标{y0}到{y1}范围内的内容，范围外仅供上下文。\n前页末尾仅供上下文（不要重复提取）：{context}\n本页原生文字（可能错序或损坏）：\n{info['text']}"
                    content = [{"type": "text", "text": text}, {"type": "image_url", "image_url": {
                        "url": "data:image/jpeg;base64," + base64.b64encode(Path(info["image_path"]).read_bytes()).decode()}}]
                    messages = [{"role": "system", "content": PAGE_PROMPT}, {"role": "user", "content": content}]
                    value = await structured_call(self.model, self.store, job_id, "page", number, messages, PageExtraction, 8000)
                    review_messages = [{"role": "system", "content": AUDIT_PROMPT + "\nPageExtraction schema:" + json.dumps(PageExtraction.model_json_schema(), ensure_ascii=False)},
                        {"role": "user", "content": [
                            {"type": "text", "text": f"PDF第{number}页，纵坐标范围{y0}—{y1}。初次转录：" + json.dumps(value, ensure_ascii=False)},
                            content[1]]}]
                    try:
                        audit = await structured_call(self.model, self.store, job_id, "page_review", number, review_messages, PageReview, 9000)
                    except Exception as exc:
                        # 原转录结构完整时，审校失败只能形成待核对资料，不能伪装成审校通过。
                        audit = {"passed": False, "corrected": None, "issues": ["模型审校未完成：" + type(exc).__name__ + "；原始转录保留，禁止自动发布。"]}
                        print(json.dumps({"event": "page_review_incomplete", "pdf_page": number, "error_type": type(exc).__name__}, ensure_ascii=False), flush=True)
                    unresolved = not audit["passed"] and audit["corrected"] is None
                    value = audit["corrected"] or value
                    # 同页存在小结和复习题等边界时，范围外内容只作上下文，不重复入库。
                    for kind in ("definitions", "teaching_blocks", "exercises", "methods", "figures"):
                        value[kind] = [item for item in value[kind] if y0 <= (item["bbox"][1] + item["bbox"][3]) / 2 <= y1]
                    result = await asyncio.to_thread(attach_page, info, value, root)
                    result["model_review"] = {"original_passed": audit["passed"], "issues_found": audit["issues"],
                        "corrected": audit["corrected"] is not None, "requires_source_review": unresolved,
                        "notice": "同一模型的独立审校调用，不代替教师或原页抽检；意见可误报，不自动当作教材错误。"}
                    print(json.dumps({"event": "page_completed", "section": section["number"], "pdf_page": number,
                        "definitions": len(result["definitions"]), "exercises": len(result["exercises"]), "issues": result["issues"]}, ensure_ascii=False), flush=True)
                    return result
            # 等所有已启动请求收束再报告失败，避免释放租约后仍有旧请求写回。
            results = await asyncio.gather(*(one(p) for p in range(section["page_start"], section["page_end"] + 1)), return_exceptions=True)
            failure = next((item for item in results if isinstance(item, BaseException)), None)
            if failure is not None:
                raise failure
            pages = results
            payload = assemble(section, pages)
            messages = [{"role": "system", "content": DESIGN_PROMPT},
                        {"role": "user", "content": json.dumps(design_input(payload), ensure_ascii=False)}]
            design = await structured_call(self.model, self.store, job_id, "teaching_design", None, messages, TeachingDesign, 5000)
            payload["teaching_design"] = validate_design(design, payload)
            for index, item in enumerate(payload["teaching_design"]["methods"], 1):
                payload["methods"].append({"source_id": f"designm{index:02}", "name": item["role"],
                    "statement": item["description"], "origin": "model_summarized", "conditions": [], "steps": [],
                    "source_refs": item["source_refs"], "evidence_source_ids": item["source_ids"]})
            payload["coverage"]["methods"] = len(payload["methods"])
            payload["quality"] = {"unresolved_model_review_pages": [p["pdf_page"] for p in pages if p["model_review"].get("requires_source_review")],
                                  "model_flagged_pages": [p["pdf_page"] for p in pages if not p["model_review"].get("original_passed")],
                                  "publication": "staged_until_source_review"}
            payload["provenance"] = {"book_id": book_id, "extractor_version": VERSION, "model": self.model.settings.llm_model,
                "vision_used": True, "ocr_model_used": False, "teacher_reviewed": False, "embedding_used": False}
            calls = self.store.calls(job_id)
            receipt = {"job_id": job_id, "model": self.model.settings.llm_model, "coverage": payload["coverage"],
                "calls": len(calls), "failed_calls": sum(c.status == "failed" for c in calls),
                "replayed_calls": sum(bool(c.metrics.get("cache_hit")) for c in calls),
                "prompt_tokens": sum(c.metrics.get("usage", {}).get("prompt_tokens", 0) for c in calls),
                "completion_tokens": sum(c.metrics.get("usage", {}).get("completion_tokens", 0) for c in calls),
                "model_duration_ms": round(sum(c.metrics.get("duration_ms", 0) for c in calls), 2)}
            self.store.commit(job_id, token, payload, receipt)
            self.export(job_id)
            print(json.dumps({"event": "section_committed", **receipt}, ensure_ascii=False), flush=True)
            return self.store.section(job_id)
        except BaseException as exc:
            self.store.fail(job_id, token, type(exc).__name__)
            raise

    def export(self, identifier):
        row = self.store.section(identifier)
        report = {"id": row.id, "book_id": row.book_id, "status": row.status, "payload_hash": row.payload_hash,
                  "review": row.review, "payload": row.payload}
        target = ROOT / "data/reports/textbook_extraction"
        target.mkdir(parents=True, exist_ok=True)
        filename = row.number.replace(":", "-")
        (target / f"section-{filename}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report

    def require_pilot_review(self, book_id, manifest):
        for candidate in self.store.sections(book_id):
            if candidate.number != "1.1" or candidate.status != "reviewed":
                continue
            row = self.store.section(candidate.id)
            provenance = row.payload.get("provenance", {})
            if (row.review.get("passed") is True and row.review.get("payload_hash") == row.payload_hash
                and provenance.get("extractor_version") == VERSION
                and provenance.get("model") == self.model.settings.llm_model
                and self.store.job(row.id).fingerprint == self.fingerprint()):
                return row
        raise ValueError("必须先完成同版本1.1原页复核，再扩展第一章。")

    async def close(self):
        await self.model.close()
