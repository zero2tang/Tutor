"""课程级查询与确定性排序，不把文本相似度当成难度或学习顺序。"""

from collections import Counter, defaultdict, deque
import math

from sqlalchemy import func, select

from app.infrastructure.persistence.database import db_session
from app.domains.courses.models import Concept, ConceptRelation, Question
from app.domains.courses.catalog import CatalogService


def graph_levels(edges: list[tuple[str, str]]) -> dict[str, int]:
    """用拓扑遍历计算最长前置链层级，有环时拒绝返回伪造次序。

    入度为零的节点从 0 开始；每条边更新子节点为 max(原层级, 父层级+1)。
    全部节点未能出队说明存在环，分类层级和先修层级都不能据此强行排序。
    """
    children, indegree, levels = defaultdict(set), Counter(), {}
    for source, target in set(edges):
        children[source].add(target)
        indegree.setdefault(source, 0)
        indegree[target] += 1
    queue = deque(sorted(node for node, degree in indegree.items() if degree == 0))
    for node in queue:
        levels[node] = 0
    visited = 0
    while queue:
        node = queue.popleft()
        visited += 1
        for child in sorted(children[node]):
            levels[child] = max(levels.get(child, 0), levels[node] + 1)
            indegree[child] -= 1
            if not indegree[child]:
                queue.append(child)
    if visited != len(indegree):
        raise ValueError("关系图存在环，不能计算可靠的前后层级")
    return levels


class CourseQueryService:
    def __init__(self, *, student_profile=None):
        """复用课程目录，为主 Agent 的课程探索和确定性排序提供事实来源。"""
        self.catalog = CatalogService()
        self.student_profile = student_profile

    def _facts(self, course_id: str):
        """汇集本课知识点、原题元信息和关系，供概览及非检索排序使用。

        此处原题投影不直接选择答案字段；知识点列表仍复用目录服务当前的
        计数实现，因此不能把该方法描述成只读取少量分页数据。
        """
        concepts = self.catalog.concepts(course_id)
        with db_session() as db:
            # 只读公开排序所需的元信息，不把标准答案带入推荐上下文。
            questions = list(
                db.execute(
                    select(
                        Question.id,
                        Question.course_id,
                        Question.concept_ids,
                        Question.validation,
                    ).where(
                        Question.course_id == course_id, Question.generated == False
                    )
                )
            )
            relations = list(
                db.execute(
                    select(
                        ConceptRelation.source_id,
                        ConceptRelation.target_id,
                        ConceptRelation.relation_type,
                        ConceptRelation.verified,
                    ).where(ConceptRelation.course_id == course_id)
                )
            )
        return concepts, questions, relations

    def overview(self, course_id: str) -> dict:
        """返回整课规模、关系类型和难度覆盖，让主 Agent 判断现有数据支持哪些任务。"""
        concepts, questions, relations = self._facts(course_id)
        kinds = Counter(row.relation_type for row in relations)
        verified_prerequisites = sum(
            row.relation_type == "PREREQUISITE_OF" and row.verified for row in relations
        )
        return {
            "course": self.catalog.course(course_id),
            "total_concepts": len(concepts),
            "teachable_concepts": sum(c["topic_type"] != "category" for c in concepts),
            "category_nodes": sum(c["topic_type"] == "category" for c in concepts),
            "original_questions": len(questions),
            "relation_counts": dict(kinds),
            "verified_prerequisite_edges": verified_prerequisites,
            "capabilities": {
                "numeric_difficulty_ranking": False,
                "prerequisite_ranking": bool(verified_prerequisites),
                "taxonomy_depth_ranking": bool(kinds["HAS_CHILD"]),
            },
            "notice": "分类、同题关联和先修关系分别解释。没有数值难度元数据时不能声称已得到最难排名。",
        }

    def search(
        self,
        course_id: str,
        *,
        query="",
        module="",
        include_categories=False,
        limit=12,
        offset=0
    ) -> dict:
        """按名称、模块或 ID 查找知识点，并返回总量、分页范围与是否还有结果。

        这是目录匹配，不使用 embedding；当前实现先读本课目录再在内存分页。
        """
        if not 1 <= limit <= 20 or not 0 <= offset <= 5000:
            raise ValueError("limit 应为 1–20，offset 应为 0–5000")
        available = self.catalog.concepts(course_id)
        candidates = [
            c for c in available if include_categories or c["topic_type"] != "category"
        ]
        if module:
            candidates = [
                c for c in candidates if module.casefold() in c["module"].casefold()
            ]
        keyword = query.strip().casefold()
        if keyword:
            candidates = [
                c
                for c in candidates
                if keyword in (c["name"] + " " + c["module"] + " " + c["id"]).casefold()
            ]
        candidates.sort(
            key=lambda c: (c["name"].casefold() != keyword, c["name"], c["id"])
        )
        items = [
            {**c, "summary": c["summary"][:500]}
            for c in candidates[offset : offset + limit]
        ]
        return {
            "course_id": course_id,
            "items": items,
            "matched_count": len(candidates),
            "total_concepts": len(available),
            "offset": offset,
            "returned_count": len(items),
            "has_more": offset + len(items) < len(candidates),
        }

    def rank(
        self, course_id: str, *, criterion: str, top_k=3, student_id="local"
    ) -> dict:
        """按明确标准比较整课学习目标，避免将检索相似度解释为难度。

        数值难度只在同一配置内比较，知识点按题目标注次数加权平均；
        先修和分类分别使用对应关系的最长链层级；个人薄弱度只使用已测证据。
        缺少数据或关系有环时返回不可用及原因，不为未知项编造分数。
        """
        if criterion not in {
            "difficulty",
            "prerequisite_depth",
            "taxonomy_depth",
            "personal_gap",
        }:
            raise ValueError("不支持的排序指标")
        if not 1 <= top_k <= 10:
            raise ValueError("top_k 应为 1–10")
        if criterion == "difficulty":
            return {
                "course_id": course_id,
                "criterion": criterion,
                "available": False,
                "items": [],
                "reason": "知识点聚合难度已退役；可使用 search_by_difficulty 查询题目。",
            }
        concepts, questions, relations = self._facts(course_id)
        eligible = {c["id"]: c for c in concepts if c["topic_type"] != "category"}
        scores, evidence = {}, {}
        result = {
            "course_id": course_id,
            "criterion": criterion,
            "higher_is_more": True,
            "eligible_count": len(eligible),
            "available": False,
            "items": [],
        }
        if criterion in {"prerequisite_depth", "taxonomy_depth"}:
            kind = (
                "PREREQUISITE_OF" if criterion == "prerequisite_depth" else "HAS_CHILD"
            )
            local_ids = {c["id"] for c in concepts}
            edges = [
                (r.source_id, r.target_id)
                for r in relations
                if r.relation_type == kind
                and r.source_id in local_ids
                and r.target_id in local_ids
                and (kind != "PREREQUISITE_OF" or r.verified)
            ]
            if not edges:
                return {
                    **result,
                    "reason": "当前课程没有可用于该指标的关系；分类或同题关联不能代替可靠先修关系。",
                }
            try:
                levels = graph_levels(edges)
            except ValueError as exc:
                return {**result, "reason": str(exc)}
            scores = {cid: level for cid, level in levels.items() if cid in eligible}
            result["basis"] = (
                "最长前置链层级"
                if kind == "PREREQUISITE_OF"
                else "分类目录层级，仅表示分类深度，不代表难度或先修顺序"
            )
        else:
            if self.student_profile is None:
                raise ValueError("个人薄弱度排序需要应用层提供当前学生画像查询能力")
            profile = self.student_profile(course_id, student_id=student_id)
            # 个人缺口为 1-已观察层级的平均掌握估计；未测层级不按 0 参与平均。
            for state in profile["states"]:
                values = [v for v in state["mastery"].values() if v is not None]
                if (
                    state["concept_id"] in eligible
                    and state["evidence_count"]
                    and values
                ):
                    scores[state["concept_id"]] = 1 - sum(values) / len(values)
                    evidence[state["concept_id"]] = {
                        "independent_questions": state["evidence_count"]
                    }
            result["basis"] = (
                "本人已观察层级掌握估计的平均缺口，未知项不参与排序，不等于课程固有难度"
            )
        ranked = sorted(scores, key=lambda cid: (-scores[cid], cid))[:top_k]
        return {
            **result,
            "available": bool(ranked),
            "scored_count": len(scores),
            "items": [
                {
                    "id": cid,
                    "name": eligible[cid]["name"],
                    "value": round(scores[cid], 4),
                    "evidence": evidence.get(cid, {}),
                }
                for cid in ranked
            ],
            "returned_count": len(ranked),
            "coverage_notice": "未获得该指标的知识点保持未知；本次排名仅覆盖 scored_count 个知识点。",
        }
