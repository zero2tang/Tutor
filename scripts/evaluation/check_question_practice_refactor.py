"""用相同真实题库与索引对照重构前后；不执行题目生成或学习状态写入。"""

import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from app.infrastructure.clients.llm import ModelClient
from app.infrastructure.clients.search import SearchClient
from app.domains.courses.retrieval.service import RetrievalService
from app.application.question_retrieval.service import QuestionRetrievalService


async def capture():
    models, search = ModelClient(), SearchClient()
    questions = QuestionRetrievalService(models, search, namespace="pilot-v1")
    retrieval = RetrievalService(models, search, None, question_retrieval=questions)
    cases = {}
    try:
        cases["exact_id"] = await questions.retrieve("math:1014", course_id="math")
        cases["text_isomorphic"] = await questions.retrieve("直线上方有三个相邻角，其中一个是直角，一个是35度，求另一个角。", course_id="math", pipeline="C")
        cases["image_isomorphic"] = await questions.retrieve("", source_question_id="math:1022", course_id="math", pipeline="C")
        cases["same_method"] = await questions.retrieve("", source_question_id="math:1015", intent="same_method", course_id="math", pipeline="C")
        cases["legacy_materials"] = await retrieval.retrieve("", course_id="mechanics", mode="keyword")
        cases["legacy_questions"] = await retrieval.retrieve("", course_id="mechanics", mode="keyword", kind="questions")
        def normalize(value):
            if isinstance(value, dict):
                return {k: sorted(v) if k in {"channels", "channels_used"} else normalize(v) for k, v in value.items()}
            return [normalize(v) for v in value] if isinstance(value, list) else value
        results = {}
        for name, result in cases.items():
            results[name] = normalize({"items": result["items"], "channels_used": result["channels_used"],
                "degraded": result["degraded"], "reason": result["reason"]})
        return results
    finally:
        await asyncio.gather(models.close(), search.close())


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--compare", type=Path)
    args = p.parse_args()
    current = asyncio.run(capture())
    report = {"results": current}
    if args.compare:
        baseline = json.loads(args.compare.read_text(encoding="utf-8"))["results"]
        report["changed_cases"] = [name for name in current if current[name] != baseline[name]]
        report["same"] = not report["changed_cases"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"cases": len(current), "same": report.get("same"), "changed": report.get("changed_cases", []),
        "ids": {name: [i["id"] for i in result["items"]] for name, result in current.items()}}, ensure_ascii=False))
    if args.compare and not report["same"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
