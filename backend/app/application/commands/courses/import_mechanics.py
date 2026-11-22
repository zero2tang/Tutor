"""导入力学教学元数据，不读取任何学生交互文件。"""

import ast
from collections import defaultdict
import csv
import hashlib
import html
import itertools
import json
from pathlib import Path
import re
import shutil

from sqlalchemy import select

from app.infrastructure.config import ROOT
from app.application.persistence import initialize_database
from app.infrastructure.persistence.database import db_session
from app.domains.courses.importing.seed_materials import MATERIALS
from app.domains.courses.models import Concept, ConceptRelation, Course, IndexTask, Material, Question


def clean(value: str) -> str:
    return re.sub(
        r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(value)))
    ).strip()


def module_for(name: str) -> str:
    if any(token in name for token in ["应力", "应变", "弹性", "屈服", "变形", "伸长"]):
        return "材料与弹性"
    if any(
        token in name
        for token in [
            "矩阵",
            "方程",
            "积分",
            "迭代",
            "指标",
            "张量",
            "函数",
            "残值",
            "系数",
        ]
    ):
        return "数学与数值方法"
    if any(
        token in name
        for token in [
            "单元",
            "有限元",
            "刚度",
            "节点",
            "自由度",
            "收敛",
            "坐标",
            "拼片",
        ]
    ):
        return "有限元方法"
    if any(token in name for token in ["热", "温度", "传热"]):
        return "热与动力学"
    return "建模与力学基础"


def import_course(source: Path) -> dict:
    initialize_database()
    destination = ROOT / "data" / "raw" / "mechanics"
    destination.mkdir(parents=True, exist_ok=True)
    # 显式白名单，禁止把整份课程目录及学生记录一起复制。
    for filename in ["concept.json", "concept2id.json", "problem.json"]:
        shutil.copyfile(source / filename, destination / filename)
    with (destination / "concept2id.json").open(encoding="utf-8", newline="") as stream:
        names = {
            name: f"mechanics:c{identifier}" for name, identifier in csv.reader(stream)
        }
    rows = [
        json.loads(line)
        for line in (destination / "problem.json")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    questions = []
    references: dict[str, list[str]] = defaultdict(list)
    edges = {}
    for row in rows:
        detail = ast.literal_eval(row["detail"])
        answer = json.loads(detail["answer"])
        concepts = [names[name] for name in row["concepts"]]
        question = {
            "id": "mechanics:" + row["problem_id"],
            "course_id": "mechanics",
            "source_id": row["problem_id"],
            "stem": clean(detail["content"]),
            "options": {
                key: clean(value) for key, value in (detail.get("option") or {}).items()
            },
            "answer": answer,
            "question_type": {
                "单选题": "choice",
                "多选题": "multi_choice",
                "填空题": "fill",
            }[detail["typetext"]],
            "cognitive_type": "unknown",
            "cognitive_dimension": row.get("cognitive_dimension"),
            "concept_ids": concepts,
            "source_ref": f"MOOCRadar C_697360 / {row['problem_id']}",
            "generated": False,
            "validation": {
                "answer_source": "original_dataset",
                "explanation_source": "not_provided",
            },
        }
        questions.append(question)
        for cid in concepts:
            references[cid].append(question["stem"])
        for a, b in itertools.combinations(sorted(set(concepts)), 2):
            identifier = hashlib.sha256(f"{a}:{b}".encode()).hexdigest()[:32]
            edges[identifier] = {
                "id": identifier,
                "course_id": "mechanics",
                "source_id": a,
                "target_id": b,
                "relation_type": "RELATED_TO",
                "origin": "question_cooccurrence",
                "verified": True,
            }
    materials = []
    summaries = {}
    for index, (title, labels, content, source_ref) in enumerate(MATERIALS):
        ids = [names[label] for label in labels if label in names]
        materials.append(
            {
                "id": f"mechanics:lesson:{index + 1}",
                "course_id": "mechanics",
                "title": title,
                "text": content,
                "concept_ids": ids,
                "source_ref": source_ref,
                "origin": "editorial",
                "visibility": "public",
            }
        )
        for cid in ids:
            summaries.setdefault(cid, content)
    for name, cid in names.items():
        context = "；".join(references[cid][:3])
        materials.append(
            {
                "id": "material:" + cid,
                "course_id": "mechanics",
                "title": name + " · 课程索引",
                "text": f"知识点：{name}。相关课程问题：{context}",
                "concept_ids": [cid],
                "source_ref": "MOOCRadar C_697360 概念及题目关联",
                "origin": "dataset_metadata",
                "visibility": "public",
            }
        )
    with db_session() as db:
        db.merge(
            Course(
                id="mechanics",
                title="力学与有限元基础",
                description="从力学建模、应力应变到有限元分析。基于 MOOCRadar C_697360 的完整教学元数据。",
                source_ref="CBEGRec / MOOCRadar C_697360",
            )
        )
        for name, cid in names.items():
            existing = db.get(Concept, cid)
            summary = summaries.get(
                cid,
                f"本课程知识点：{name}。可结合关联题目、知识图谱与教学 Agent 继续学习。",
            )
            if existing is None:
                db.add(
                    Concept(
                        id=cid,
                        course_id="mechanics",
                        source_id=cid.split(":c")[-1],
                        name=name,
                        module=module_for(name),
                        summary=summary,
                    )
                )
        for question in questions:
            if db.get(Question, question["id"]) is None:
                db.add(Question(**question))
        for material in materials:
            db.merge(Material(**material))
        for edge in edges.values():
            db.merge(ConceptRelation(**edge))
        for target in ["es", "neo4j"]:
            db.merge(
                IndexTask(
                    id=f"mechanics:v1:{target}",
                    target=target,
                    status="pending",
                    details={"course_id": "mechanics"},
                )
            )
    report = {
        "course_id": "mechanics",
        "concepts": len(names),
        "questions": len(questions),
        "materials": len(materials),
        "relations": len(edges),
        "historical_interaction_files": 0,
    }
    (ROOT / "data" / "reports" / "mechanics_import.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report
