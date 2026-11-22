"""汇总真实提取回执、主库一致性与原页对照；不把模型自审当成正确率。"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
from html import escape
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import select
from app.application.model_registry import register_models
from app.domains.materials.extraction.store import ExtractionStore
from app.domains.materials.extraction.models import ExtractedSection
from app.infrastructure.persistence.model_calls import ModelCall
from app.infrastructure.documents.pdf import digest
from app.application.textbooks.prompts import VERSION


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--book-id", required=True)
    args = parser.parse_args()
    register_models()
    store = ExtractionStore()
    book = store.book(args.book_id)
    rows = store.sections(book.id)
    selected = {}
    for row in rows:
        if row.payload.get("provenance", {}).get("extractor_version") != VERSION or row.status not in {"extracted", "reviewed"}:
            continue
        previous = selected.get(row.number)
        if previous is None or (row.status == "reviewed" and previous.status != "reviewed"):
            selected[row.number] = store.section(row.id)
    selected = sorted(selected.values(), key=lambda r: (r.page_start, r.number))
    checks = []
    def check(label, condition, details=None):
        checks.append({"label": label, "passed": bool(condition), "details": details})
    expected = {s["number"] for s in book.manifest["sections"]}
    check("第一章目录单元全部完成且没有后续章节", {r.number for r in selected} == expected)
    check("整书原件SHA256与数据库一致", hashlib.sha256(book.raw_content).hexdigest() == book.source_hash)
    covered = {p["pdf_page"] for r in selected for p in r.payload["pages"]}
    check("第一章源页覆盖完整", covered == set(range(book.manifest["chapter_start"], book.manifest["chapter_end"] + 1)), sorted(covered))
    pilot = next((r for r in selected if r.number == "1.1"), None)
    check("1.1原页复核回执与当前内容指纹一致", pilot and pilot.status == "reviewed" and pilot.review.get("passed") is True and pilot.review.get("payload_hash") == pilot.payload_hash)
    refs, unresolved, resources = 0, [], 0
    for row in selected:
        resource_expected = sum(len(row.payload[k]) for k in ("definitions", "exercises", "methods")) + 1
        check(f"{row.number}三类资源与教学模式的小节快照指纹一致", digest(row.payload) == row.payload_hash)
        resources += resource_expected
        def inspect(value):
            nonlocal refs
            if isinstance(value, dict):
                for key, part in value.items():
                    if key == "source_refs":
                        for ref in part:
                            refs += 1
                            if not book.manifest["chapter_start"] <= ref["pdf_page"] <= book.manifest["chapter_end"]:
                                unresolved.append(ref)
                    else:
                        inspect(part)
            elif isinstance(value, list):
                for part in value:
                    inspect(part)
        inspect(row.payload)
        check(f"{row.number}图片资产可恢复", all(Path(f["asset_path"]).exists() for f in row.payload["figures"]))
    check("所有来源页均在授权第一章范围", not unresolved, {"source_refs": refs})
    with store.session() as db:
        jobs = list(db.scalars(select(ExtractedSection).where(ExtractedSection.book_id == book.id)))
        calls = list(db.scalars(select(ModelCall).where(ModelCall.resource_id.in_([j.id for j in jobs]))))
    actual = [c for c in calls if not c.metrics.get("cache_hit")]
    check("任务与调用均已收束", not any(j.extraction_status == "running" for j in jobs) and not any(c.status == "running" for c in calls))
    check("没有对后续章节发送模型请求", all(c.page is None or 8 <= c.page <= 59 for c in calls))
    totals = Counter()
    for row in selected:
        totals.update({key: len(row.payload[key]) for key in ("definitions", "exercises", "methods", "figures")})
    report = {"complete": all(c["passed"] for c in checks), "generated_at": datetime.now(timezone.utc).isoformat(),
        "book_id": book.id, "source_sha256": book.source_hash, "course_id": book.course_id,
        "scope": {"chapter": 1, "pdf_pages": [8, 59], "printed_pages": [1, 52], "later_chapters_processed": False},
        "pipeline_version": VERSION, "sections": [{"id": r.id, "number": r.number, "title": r.title, "status": r.status,
            "hash": r.payload_hash, "coverage": r.payload["coverage"], "page_review_corrections": sum(p.get("model_review", {}).get("corrected", False) for p in r.payload["pages"])} for r in selected],
        "review_state": {"source_reviewed_sections": [r.number for r in selected if r.status == "reviewed"],
            "staged_sections": [r.number for r in selected if r.status != "reviewed"],
            "model_review_disagreement_pages": sorted({p["pdf_page"] for r in selected for p in r.payload["pages"] if p.get("model_review", {}).get("requires_source_review")})},
        "totals": {**totals, "sections": len(selected), "source_pages": len(covered), "resources_in_section_payload": resources, "source_refs": refs},
        "metrics_including_initial_failures": {"model_calls": len(actual), "failed_calls": sum(c.status == "failed" for c in actual),
            "replayed_calls": len(calls) - len(actual),
            "prompt_tokens": sum(c.metrics.get("usage", {}).get("prompt_tokens", 0) for c in actual),
            "completion_tokens": sum(c.metrics.get("usage", {}).get("completion_tokens", 0) for c in actual),
            "model_duration_ms_sum": round(sum(c.metrics.get("duration_ms", 0) for c in actual), 2),
            "upstream_attempts": sum((c.metrics.get("request_metrics") or c.response.get("raw", {}).get("_request_metrics", {})).get("request_attempts", 1) for c in actual),
            "model_names": sorted({c.model for c in actual}),
            "response_model_names": sorted({c.response.get("raw", {}).get("model") for c in actual if c.response.get("raw", {}).get("model")}),
            "failure_types": Counter(c.error.split(":")[0] for c in actual if c.status == "failed")},
        "checks": checks, "limitations": ["complete表示范围、来源和存储检查完成，不表示逐字提取正确率或学习收益。", "1.1经过Codex逐页复核与有记录的原页更正；第一章其他单元为真实模型提取及同模型独立审校，仍待原页抽检。", "模型坐标是估计区域；图示描述不能替代原图中的数学条件。", "未建立向量索引，未处理第二章以后。"]}
    output = ROOT / "data/reports/textbook_extraction"
    output.mkdir(parents=True, exist_ok=True)
    (output / "chapter-1-acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    html = ['<!doctype html><meta charset="utf-8"><title>教材第一章原页对照</title><style>body{font:15px/1.7 system-ui;margin:24px;background:#f5f6f8;color:#18212c}h1,h2{color:#193b5a}article{display:grid;grid-template-columns:minmax(400px,1fr) minmax(400px,1fr);gap:24px;margin-bottom:30px}img{width:100%;border:1px solid #ccc}section,aside{background:white;padding:18px}pre{white-space:pre-wrap;overflow-wrap:anywhere}small{color:#687887}.tag{background:#edf1f7;padding:3px 8px}details{margin:12px 0}</style>',
        '<h1>教材第一章：原页与三类提取结果</h1><p>左侧为Poppler原页，右侧为当前数据库快照。1.1已逐页复核；其余为模型提取与模型审校版本。模型审校意见可能误报，不能当作教材事实。</p>']
    for row in selected:
        payload = row.payload
        html.append(f'<h2>{escape(row.number)} {escape(row.title)} <small>{escape(row.status)}</small></h2><p>{escape(payload["teaching_design"]["summary"])}</p>')
        for page in payload["pages"]:
            source = Path(book.asset_root) / f'page-{page["pdf_page"]:03}-poppler.jpg'
            image_url = os.path.relpath(source, output).replace(os.sep, "/")
            html.append(f'<article><section><h3>PDF第{page["pdf_page"]}页／书内第{escape(page["printed_page"])}页</h3><img loading="lazy" src="{escape(image_url, quote=True)}"></section><aside>')
            for kind, title in (("definitions", "定义与说明"), ("exercises", "习题"), ("methods", "方法")):
                items = [item for item in payload[kind] if any(ref["pdf_page"] == page["pdf_page"] for ref in item.get("source_refs", []))]
                if items:
                    html.append(f'<h3>{title}</h3>')
                for item in items:
                    html.append('<details open><summary>' + escape(item.get("name") or f'{item.get("group") or item.get("kind")} {item.get("number")}') + '</summary><pre>' + escape(json.dumps(item, ensure_ascii=False, indent=2)) + '</pre></details>')
            html.append('<details><summary>模型审校回执（可能误报）</summary><pre>' + escape(json.dumps(page.get("model_review"), ensure_ascii=False, indent=2)) + '</pre></details></aside></article>')
    (output / "chapter-1-review.html").write_text("\n".join(html), encoding="utf-8")
    print(json.dumps({"complete": report["complete"], "totals": report["totals"], "metrics": report["metrics_including_initial_failures"], "failed_checks": [c for c in checks if not c["passed"]]}, ensure_ascii=False))
    if not report["complete"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
