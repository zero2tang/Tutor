"""导入 XES3G5M 教学元数据；不读取任何历史学生交互。"""

from collections import Counter, defaultdict
import hashlib
import html
from html.parser import HTMLParser
import itertools
import json
from pathlib import Path
import re

from app.infrastructure.config import ROOT
from app.application.persistence import initialize_database
from app.infrastructure.persistence.database import db_session
from app.domains.courses.models import Concept, ConceptRelation, Course, IndexTask, Material, Question

DEFAULT_SOURCE = ROOT / "data" / "raw" / "xes3g5m" / "metadata"
IMAGE_TOKEN = re.compile(
    r"(?<![\w/-])((?:question|analysis)_\d+-image_\d+)(?:\.png)?(?!\w)"
)
IMAGE_NAME = re.compile(r"(?:question|analysis)_\d+-image_\d+\.png\Z")


class _SafeContent(HTMLParser):
    """仅保留教学排版标签和本地数据集图片，避免执行原始 HTML。"""

    safe_tags = {
        "p",
        "br",
        "div",
        "span",
        "b",
        "strong",
        "i",
        "em",
        "sub",
        "sup",
        "ul",
        "ol",
        "li",
        "table",
        "thead",
        "tbody",
        "tr",
        "td",
        "th",
    }
    blocked_tags = {"script", "style", "iframe", "object", "svg", "math"}
    block_tags = {"p", "div", "br", "li", "tr"}

    def __init__(self, images_dir: Path):
        super().__init__(convert_charrefs=True)
        self.images_dir = images_dir
        self.markdown_parts: list[str] = []
        self.html_parts: list[str] = []
        self.images: list[str] = []
        self.missing_images: list[str] = []
        self.blocked_depth = 0
        self.open_tags: list[str] = []
        self.external_image_count = 0

    def image(self, name: str) -> None:
        if name not in self.images:
            self.images.append(name)
        if not (self.images_dir / name).is_file() and name not in self.missing_images:
            self.missing_images.append(name)
        url = "/api/assets/math/" + name
        self.markdown_parts.append(f"\n![题目配图]({url})\n")
        self.html_parts.append(f'<img src="{url}" alt="题目配图">')

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.blocked_tags:
            self.blocked_depth += 1
            return
        if self.blocked_depth:
            return
        if tag == "img":
            source = dict(attrs).get("src") or ""
            # 不接收 URL、绝对路径或目录穿越；资源必须来自下载的白名单文件。
            name = source.removeprefix("images/").removeprefix("/api/assets/math/")
            if IMAGE_NAME.fullmatch(name):
                self.image(name)
            else:
                self.external_image_count += 1
            return
        if tag not in self.safe_tags:
            return
        self.html_parts.append(f"<{tag}>")
        if tag != "br":
            self.open_tags.append(tag)
        if tag in self.block_tags:
            self.markdown_parts.append("\n")
        if tag == "li":
            self.markdown_parts.append("- ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in {"img", "br"}:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.blocked_tags:
            self.blocked_depth = max(0, self.blocked_depth - 1)
            return
        if self.blocked_depth or tag not in self.open_tags:
            return
        # 补齐不平衡的安全标签，防止跨组件影响页面排版。
        while self.open_tags:
            closing = self.open_tags.pop()
            self.html_parts.append(f"</{closing}>")
            if closing == tag:
                break
        if tag in self.block_tags:
            self.markdown_parts.append("\n")

    def handle_data(self, value: str) -> None:
        if self.blocked_depth:
            return
        offset = 0
        for match in IMAGE_TOKEN.finditer(value):
            self.text(value[offset : match.start()])
            self.image(match.group(1) + ".png")
            offset = match.end()
        self.text(value[offset:])

    def text(self, value: str) -> None:
        # HTMLParser 已剥离真实标签；保留 LaTeX 的不等号，页面不执行 Markdown 内的原始 HTML。
        self.markdown_parts.append(value)
        self.html_parts.append(html.escape(value, quote=False))

    def result(self) -> dict:
        while self.open_tags:
            self.html_parts.append(f"</{self.open_tags.pop()}>")
        markdown = re.sub(r"\n[ \t]+", "\n", "".join(self.markdown_parts))
        markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()
        return {
            "markdown": markdown,
            "html": "".join(self.html_parts).strip(),
            "images": self.images,
            "missing_images": self.missing_images,
            "external_image_count": self.external_image_count,
        }


def sanitize_content(value: str, images_dir: Path) -> dict:
    parser = _SafeContent(images_dir)
    # 先保护公式中的 < >，否则 HTMLParser 会把 x<y...> 当成标签并吞掉公式。
    # 解析器会把实体还原为文本，因此 Markdown 仍保留原始数学符号。
    protected = re.sub(
        r"\$\$.*?\$\$|(?<!\$)\$(?!\$).*?(?<!\$)\$(?!\$)",
        lambda match: html.escape(match.group(), quote=False),
        str(value),
        flags=re.DOTALL,
    )
    parser.feed(protected)
    parser.close()
    return parser.result()


def _relation(source: str, target: str, kind: str, origin: str) -> dict:
    key = hashlib.sha256(f"math:{kind}:{source}:{target}".encode()).hexdigest()[:32]
    return {
        "id": "math:" + key,
        "course_id": "math",
        "source_id": source,
        "target_id": target,
        "relation_type": kind,
        "origin": origin,
        "verified": True,
    }


def prepare_course(source: Path | None = None) -> dict:
    """纯解析入口：生成可审核记录，不连接数据库或模型服务。"""
    source = Path(source or DEFAULT_SOURCE)
    question_file, route_file = source / "questions.json", source / "kc_routes_map.json"
    raw_questions = json.loads(question_file.read_text(encoding="utf-8"))
    raw_nodes = json.loads(route_file.read_text(encoding="utf-8"))
    if not isinstance(raw_questions, dict) or not isinstance(raw_nodes, dict):
        raise ValueError("XES3G5M 元数据必须为以原始 ID 为键的 JSON 对象")
    if len(set(raw_nodes.values())) != len(raw_nodes):
        raise ValueError("知识映射有重名节点，不能按名称无损解析完整路径")
    names = {name: f"math:c{identifier}" for identifier, name in raw_nodes.items()}
    paths: dict[str, set[tuple[str, ...]]] = defaultdict(set)
    leaf_ids: set[str] = set()
    relations: dict[str, dict] = {}
    questions: list[dict] = []
    image_references: set[str] = set()
    missing_images: set[str] = set()
    missing_question_ids: list[str] = []
    question_types: Counter = Counter()
    availability_counts: Counter = Counter()
    quarantined_question_ids: dict[str, list[str]] = defaultdict(list)
    images_dir = source / "images"

    for identifier, row in raw_questions.items():
        routes = row.get("kc_routes")
        if not isinstance(routes, list) or not routes:
            raise ValueError(f"题目 {identifier} 缺少知识路径")
        route_ids, concepts = [], []
        for route in routes:
            # 前后空格是官方节点标识的一部分，不能 strip 后合并两个不同 ID。
            labels = route.split("----")
            try:
                ids = [names[label] for label in labels]
            except KeyError as exc:
                raise ValueError(
                    f"题目 {identifier} 引用了知识映射中不存在的节点"
                ) from exc
            route_ids.append([cid.removeprefix("math:c") for cid in ids])
            concepts.append(ids[-1])
            leaf_ids.add(ids[-1])
            for offset, cid in enumerate(ids):
                paths[cid].add(tuple(labels[: offset + 1]))
            for parent, child in zip(ids, ids[1:]):
                relation = _relation(parent, child, "HAS_CHILD", "dataset_taxonomy")
                relations[relation["id"]] = relation
        concepts = list(dict.fromkeys(concepts))
        for a, b in itertools.combinations(sorted(concepts), 2):
            relation = _relation(a, b, "RELATED_TO", "question_cooccurrence")
            relations[relation["id"]] = relation
        stem = sanitize_content(row["content"], images_dir)
        options = {
            key: sanitize_content(value, images_dir)
            for key, value in row.get("options", {}).items()
        }
        explanation = sanitize_content(row.get("analysis", ""), images_dir)
        public_images = list(
            dict.fromkeys(
                stem["images"]
                + [name for value in options.values() for name in value["images"]]
            )
        )
        public_missing = list(
            dict.fromkeys(
                stem["missing_images"]
                + [
                    name
                    for value in options.values()
                    for name in value["missing_images"]
                ]
            )
        )
        image_references.update(public_images + explanation["images"])
        missing_images.update(public_missing + explanation["missing_images"])
        if public_missing:
            missing_question_ids.append("math:" + str(identifier))
        question_type = {"填空": "fill", "单选": "choice"}.get(row["type"])
        if question_type is None:
            raise ValueError(f"题目 {identifier} 包含尚未处理的题型 {row['type']}")
        question_types[question_type] += 1
        option_refs = [name for value in options.values() for name in value["images"]]
        availability, availability_note = "ready", ""
        if public_missing:
            availability = "missing_figure"
            availability_note = "原始题干或选项引用的图片缺失，暂不用于独立练习。"
        elif question_type == "choice" and len(options) < 2:
            availability = "insufficient_options"
            availability_note = "原始单选题不足两个选项，暂不用于独立练习。"
        elif (
            question_type == "choice"
            and option_refs
            and len(set(option_refs)) < len(option_refs)
        ):
            availability = "ambiguous_options"
            availability_note = "原始选择题多个选项重复同一图片，图片映射无法可靠区分，暂不用于独立练习。"
        elif question_type == "choice" and len(
            set(row.get("options", {}).values())
        ) < len(options):
            availability = "duplicate_options"
            availability_note = "原始选择题存在重复干扰选项，暂不用于独立练习。"
        elif question_type == "choice" and (
            not isinstance(row["answer"], list)
            or len(row["answer"]) != 1
            or row["answer"][0] not in options
        ):
            availability = "invalid_answer"
            availability_note = "原始单选题答案无法对应唯一选项，暂不用于独立练习。"
        availability_counts[availability] += 1
        if availability != "ready":
            quarantined_question_ids[availability].append("math:" + str(identifier))
        original_answer = row["answer"]
        # 原数据填空列表的每项对应一个空，不能按单空的多个可接受答案处理。
        ordered_blanks = (
            question_type == "fill"
            and isinstance(original_answer, list)
            and len(original_answer) > 1
        )
        answer = (
            {str(index + 1): [value] for index, value in enumerate(original_answer)}
            if ordered_blanks
            else original_answer
        )
        question = {
            "id": "math:" + str(identifier),
            "course_id": "math",
            "source_id": str(identifier),
            "stem": stem["markdown"],
            "options": {key: value["markdown"] for key, value in options.items()},
            "answer": answer,
            "explanation": explanation["markdown"],
            "question_type": question_type,
            "cognitive_type": "unknown",
            "concept_ids": concepts,
            "source_ref": f"XES3G5M / metadata/questions.json / {identifier}",
            "generated": False,
            "validation": {
                "answer_source": "original_dataset",
                "explanation_source": "original_dataset",
                "answer_structure": (
                    "ordered_blanks" if ordered_blanks else "single_answer"
                ),
                "blank_count": len(original_answer) if question_type == "fill" else 0,
                "source_type": row["type"],
                "source_content": row["content"],
                "source_options": row.get("options", {}),
                "stem_format": "markdown",
                "stem_html": stem["html"],
                "options_html": {key: value["html"] for key, value in options.items()},
                "knowledge_routes": routes,
                "knowledge_route_ids": route_ids,
                "images": public_images,
                "analysis_images": explanation["images"],
                "missing_images": public_missing,
                "figure_available": bool(public_images) and not public_missing,
                "availability": availability,
                "availability_note": availability_note,
            },
        }
        questions.append(question)

    concepts, materials = [], []
    for identifier, name in raw_nodes.items():
        cid = names[name]
        node_paths = sorted(paths[cid])
        summary = "知识分类路径：" + "；".join(" → ".join(path) for path in node_paths)
        concepts.append(
            {
                "id": cid,
                "course_id": "math",
                "source_id": str(identifier),
                "name": name,
                "topic_type": "concept" if cid in leaf_ids else "category",
                "summary": summary,
                "module": node_paths[0][0] if node_paths else "未归类",
            }
        )
        # 公共检索材料仅包含分类事实，不混入标准答案或题目解析。
        materials.append(
            {
                "id": "material:" + cid,
                "course_id": "math",
                "title": name + " · 数学知识索引",
                "text": f"知识节点：{name}。{summary}。分类父子关系表示教材结构，不代表先修依赖。",
                "concept_ids": [cid],
                "source_ref": f"XES3G5M / metadata/kc_routes_map.json / {identifier}",
                "origin": "dataset_metadata",
                "visibility": "public",
            }
        )
    report = {
        "course_id": "math",
        "source": "XES3G5M teaching metadata",
        "questions": len(questions),
        "question_types": dict(question_types),
        "concepts": len(concepts),
        "hierarchy_nodes": len(concepts),
        "hierarchy_names_stripped": len({name.strip() for name in raw_nodes.values()}),
        "leaf_concept_ids": len(leaf_ids),
        "leaf_names_exact": len(
            {raw_nodes[cid.removeprefix("math:c")] for cid in leaf_ids}
        ),
        "leaf_names_stripped": len(
            {raw_nodes[cid.removeprefix("math:c")].strip() for cid in leaf_ids}
        ),
        "category_nodes": len(concepts) - len(leaf_ids),
        "materials": len(materials),
        "relations": len(relations),
        "relations_by_type": dict(
            Counter(row["relation_type"] for row in relations.values())
        ),
        "downloaded_images": (
            sum(path.is_file() for path in images_dir.iterdir())
            if images_dir.is_dir()
            else 0
        ),
        "referenced_images": len(image_references),
        "missing_images": sorted(missing_images),
        "missing_figure_question_ids": missing_question_ids,
        "availability_counts": dict(availability_counts),
        "quarantined_question_ids": dict(quarantined_question_ids),
        "independent_practice_questions": availability_counts["ready"],
        "historical_interaction_files": 0,
        "source_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (question_file, route_file)
        },
    }
    return {
        "course": {
            "id": "math",
            "title": "小学数学与拓展思维",
            "description": "XES3G5M 小学数学教学元数据：知识分类、题目、原始答案解析与配图；不包含历史学生交互。",
            "source_ref": "XES3G5M / https://github.com/ai4ed/XES3G5M",
        },
        "concepts": concepts,
        "questions": questions,
        "materials": materials,
        "relations": list(relations.values()),
        "report": report,
    }


def import_course(source: Path | None = None) -> dict:
    """幂等写入来源记录；检索投影由统一构建器继续生成。"""
    bundle = prepare_course(source)
    initialize_database()
    with db_session() as db:
        db.merge(Course(**bundle["course"]))
        for model, key in (
            (Concept, "concepts"),
            (Question, "questions"),
            (Material, "materials"),
            (ConceptRelation, "relations"),
        ):
            for record in bundle[key]:
                db.merge(model(**record))
        for target in ("es", "neo4j"):
            db.merge(
                IndexTask(
                    id=f"math:v1:{target}",
                    target=target,
                    status="pending",
                    details={"course_id": "math"},
                )
            )
    destination = ROOT / "data" / "reports" / "math_import.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(bundle["report"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return bundle["report"]
