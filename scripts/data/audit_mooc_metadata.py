"""只读取三门课程教学元数据，输出结构与内容完整性审计。"""

from __future__ import annotations

import argparse
import ast
from collections import Counter, deque
import csv
import json
from pathlib import Path
import sys


COURSES = {"Logistics": "逻辑学", "Mechanical_Physics": "力学", "PHP": "PHP"}


def read_json_lines(path: Path) -> list:
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def graph_summary(graph: dict, valid_ids: set[str]) -> dict:
    # 源文件以当前知识点为键，以其先修列表为值；审计不等于认可关系语义。
    adjacency: dict[str, list[str]] = {node: [] for node in valid_ids}
    indegree = dict.fromkeys(valid_ids, 0)
    invalid = []
    edges = 0
    for target, prerequisites in graph.items():
        for prerequisite in prerequisites:
            source = str(prerequisite)
            if target not in valid_ids or source not in valid_ids:
                invalid.append([source, target])
                continue
            edges += 1
            adjacency[source].append(target)
            indegree[target] += 1
    queue = deque(node for node, count in indegree.items() if count == 0)
    visited = 0
    while queue:
        node = queue.popleft()
        visited += 1
        for child in adjacency[node]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    return {"edges": edges, "invalid_endpoints": invalid, "is_dag": visited == len(valid_ids), "provenance": "legacy_inferred_not_verified"}


def audit_course(folder: Path, display_name: str) -> dict:
    concepts = read_json_lines(folder / "concept.json")
    questions = read_json_lines(folder / "problem.json")
    info = json.loads((folder / "problem_info.json").read_text(encoding="utf-8"))
    problem_mapping = json.loads((folder / "problem2id.json").read_text(encoding="utf-8"))
    # 三门课程的 concept2id 均为 CSV；反向映射文件在不同课程中格式并不统一。
    with (folder / "concept2id.json").open(encoding="utf-8", newline="") as stream:
        concept_mapping = {row[1]: row[0] for row in csv.reader(stream) if row}
    graph = json.loads((folder / "KG_structure.json").read_text(encoding="utf-8"))
    counts = Counter()
    types = Counter()
    ids = set()
    course_ids = set()
    errors = []
    for question in questions:
        qid = question["problem_id"]
        counts["duplicate_question_ids"] += qid in ids
        ids.add(qid)
        course_ids.add(question["course_id"])
        try:
            detail = ast.literal_eval(question["detail"])
            if not isinstance(detail, dict):
                raise ValueError("题目详情不是字典")
        except (ValueError, SyntaxError) as exc:
            errors.append({"question_id": qid, "error": type(exc).__name__})
            continue
        counts["parseable_details"] += 1
        for field in ["content", "answer", "option", "analysis", "explanation"]:
            counts[field] += bool(detail.get(field))
        types[str(detail.get("typetext", "unknown"))] += 1
        mapped = problem_mapping.get(qid)
        record = info.get(str(mapped), {})
        if record and record.get("difficulty") == question.get("cognitive_dimension"):
            counts["difficulty_equal_to_cognitive_dimension"] += 1
    return {
        "display_name": display_name,
        "source_directory": str(folder.resolve()),
        "source_course_ids": sorted(course_ids),
        "concepts": len(concepts),
        "questions": len(questions),
        "field_counts": dict(counts),
        "question_types": dict(types),
        "parse_errors": errors,
        "graph": graph_summary(graph, set(concept_mapping)),
    }


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("data/reports/mooc_metadata_audit.json"))
    args = parser.parse_args()
    report = {
        "scope": "教学元数据，不读取历史交互",
        "courses": [audit_course(args.source / name, display) for name, display in COURSES.items()],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
