"""真实 LangChain 工具：类型声明生成唯一 Schema，业务调用使用可信运行范围。"""

from typing import Annotated, Literal

from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.agent.runtime.contracts import RuntimeContext
from app.agent.runtime.skills import SkillLibrary
from app.domains.courses.catalog import CatalogService
from app.domains.courses.queries import CourseQueryService
from app.domains.courses.difficulty.service import DifficultyQueryService
from app.domains.materials.resources.service import TeachingResourceService
from app.domains.practice.context import SessionExerciseService
from app.domains.practice.schemas import ExerciseBatchRequest
from app.domains.practice.requirements import validate_generation_operation
from app.domains.students.service import StudentService
from app.infrastructure.persistence.database import db_session
from app.shared.concurrency import run_blocking

ConceptID = Annotated[str, StringConstraints(min_length=1, max_length=96)]
Runtime = ToolRuntime[RuntimeContext]


class KnowledgeArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    task: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    concept_ids: list[ConceptID] = Field(min_length=1, max_length=6)


class ExerciseArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation: Literal["explain", "select", "generate", "batch"]
    task: str = Field(default="", max_length=2000)
    concept_ids: list[ConceptID] = Field(min_length=1, max_length=6)
    exercise_id: Annotated[str, Field(pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")] | None = None
    cognitive_type: Literal["recall", "understanding", "application"] = "understanding"
    exercise_batch: ExerciseBatchRequest | None = None

    @model_validator(mode="after")
    def executable_operation(self):
        validate_generation_operation(
            self.operation, len(self.exercise_batch.tasks) if self.exercise_batch else None
        )
        return self


class KnowledgeToolInput(KnowledgeArguments):
    model_config = ConfigDict(extra="forbid", strict=True, arbitrary_types_allowed=True)
    runtime: Runtime


class ExerciseToolInput(ExerciseArguments):
    model_config = ConfigDict(extra="forbid", strict=True, arbitrary_types_allowed=True)
    runtime: Runtime


ROLE_TOOLS = {
    "supervisor": {"get_concept_profile", "get_concept_observations", "get_session_goal", "list_session_exercises", "get_session_exercise", "get_difficulty_details", "search_by_difficulty", "read_skill", "get_course_overview", "search_concepts", "rank_learning_targets", "get_concept_details", "get_knowledge_structure", "rag_retrieve", "read_student_state", "call_knowledge_agent", "call_exercise_agent"},
    "knowledge": {"get_concept_profile", "get_concept_observations", "get_session_goal", "search_teaching_resources", "read_teaching_resource", "get_teaching_analysis", "get_difficulty_details", "search_by_difficulty", "read_skill", "get_concept_details", "get_knowledge_structure", "search_exercises", "rag_retrieve", "graph_rag_retrieve", "retrieve_learning_memory", "call_exercise_agent"},
    "exercise": {"get_concept_profile", "search_teaching_resources", "read_teaching_resource", "get_teaching_analysis", "get_difficulty_details", "search_by_difficulty", "read_skill", "search_exercises", "get_concept_details", "get_knowledge_structure", "rag_retrieve"},
    "assessment": {"read_skill", "get_concept_details", "read_student_state", "graph_rag_retrieve"},
}


def compact_tool_result(result, *, tool_name=None):
    """只裁检索摘录；原题、原答及已读取技能规则保持完整。"""
    if tool_name not in {"get_concept_details", "rag_retrieve", "graph_rag_retrieve", "search_concepts"}:
        return result
    changed = False

    def trim(value, key=""):
        nonlocal changed
        limit = 3000 if key in {"text", "summary"} else 10000
        if isinstance(value, str) and len(value) > limit:
            changed = True
            return value[:limit] + "\n[内容已截断]"
        if isinstance(value, list):
            size = 30 if key == "edges" else 20
            changed |= len(value) > size
            return [trim(item) for item in value[:size]]
        if isinstance(value, dict):
            return {name: trim(item, name) for name, item in value.items()}
        return value

    value = trim(result)
    if changed:
        value = {**value, "context_truncated": True, "scope_notice": "这是工具结果的部分内容，不能据此推断全课程只有这些条目。"}
    return value


class TeachingTools:
    """只装配服务和真实工具，不按字符串名称分派领域业务。"""

    def __init__(self, retrieval):
        self.retrieval = retrieval
        self.catalog, self.student = CatalogService(), StudentService()
        self.queries = CourseQueryService(student_profile=self.student.profile)
        self.skills, self.difficulty = SkillLibrary(), DifficultyQueryService()
        self.teaching_resources = TeachingResourceService()
        self._tools = self._build()

    def for_role(self, role, *, handler=None):
        return [item for item in self._tools if item.name in ROLE_TOOLS[role]
                and (handler is not None or not item.name.startswith("call_"))]

    def _validate(self, scope, concepts=(), *, session=False):
        self.catalog.course(scope.course_id)
        for cid in dict.fromkeys([*scope.concept_ids, *concepts]):
            self.catalog.concept(cid, scope.course_id)
        if session and not scope.session_id:
            raise ValueError("读取会话数据需要当前会话")

    async def _read(self, runtime, function, *args, concepts=(), session=False, **kwargs):
        scope = runtime.context.scope

        def query():
            self._validate(scope, concepts, session=session)
            return function(*args, **kwargs)

        return await run_blocking(query)

    def _build(self):
        @tool
        async def read_skill(name: Annotated[str, Field(max_length=80)], runtime: Runtime):
            """读取本角色专业技能；同一运行不重复读取已加载规则。"""
            return self.skills.read(runtime.context.role, name)

        @tool
        async def get_course_overview(runtime: Runtime):
            """读取整门课程的知识点、题目、关系及难度数据覆盖。"""
            return await self._read(runtime, self.queries.overview, runtime.context.scope.course_id)

        @tool
        async def search_concepts(runtime: Runtime, query: str = "", module: str = "", include_categories: bool = False,
                                  limit: Annotated[int, Field(ge=1, le=20)] = 12,
                                  offset: Annotated[int, Field(ge=0, le=5000)] = 0):
            """查课程目录或按名称匹配知识点；局部列表不代表整门课程。"""
            return await self._read(runtime, self.queries.search, runtime.context.scope.course_id,
                                    query=query, module=module, include_categories=include_categories, limit=limit, offset=offset)

        @tool
        async def rank_learning_targets(criterion: Literal["prerequisite_depth", "taxonomy_depth", "personal_gap"], runtime: Runtime,
                                        top_k: Annotated[int, Field(ge=1, le=10)] = 3):
            """按可靠先修层级、分类深度或本人已测缺口排名，缺数据返回未知。"""
            scope = runtime.context.scope
            return await self._read(runtime, self.queries.rank, scope.course_id, criterion=criterion, top_k=top_k, student_id=scope.student_id)

        @tool
        async def get_concept_details(concept_id: ConceptID, runtime: Runtime):
            """读取本课程知识点定义、教学卡片和可追溯来源。"""
            return await self._read(runtime, self.catalog.concept, concept_id, runtime.context.scope.course_id, concepts=[concept_id])

        @tool
        async def get_concept_profile(concept_id: ConceptID, runtime: Runtime):
            """读取当前教学范围内知识点的正式状态与有来源的语义记忆。"""
            from app.domains.memory.concepts.queries import concept_memory_query
            return await self._read(runtime, concept_memory_query, self.student, runtime.context.scope, concept_id,
                                    concepts=[concept_id], observations=False, limit=3)

        @tool
        async def get_concept_observations(concept_id: ConceptID, runtime: Runtime,
                                           limit: Annotated[int, Field(ge=1, le=3)] = 3):
            """下钻当前教学范围内的跨会话观察与帮助条件，不授予掌握结论。"""
            from app.domains.memory.concepts.queries import concept_memory_query
            return await self._read(runtime, concept_memory_query, self.student, runtime.context.scope, concept_id,
                                    concepts=[concept_id], observations=True, limit=limit)

        @tool
        async def get_session_goal(goal_id: str, runtime: Runtime):
            """读取当前会话的目标及成功标准，不能跨会话读取。"""
            from app.domains.sessions.models import LearningSession
            from app.domains.sessions.goals import get_session_goal as query_goal
            scope = runtime.context.scope

            def query():
                with db_session() as db:
                    row = db.get(LearningSession, scope.session_id)
                    if row is None or (row.student_id, row.course_id) != (scope.student_id, scope.course_id):
                        raise PermissionError("目标读取范围与当前学习会话不一致")
                    return query_goal(db, row, goal_id)

            return await self._read(runtime, query, session=True)

        @tool
        async def list_session_exercises(runtime: Runtime, query: Annotated[str, Field(max_length=200)] = "",
                                         limit: Annotated[int, Field(ge=1, le=10)] = 6,
                                         offset: Annotated[int, Field(ge=0, le=10000)] = 0):
            """倒序查询当前会话历史练习的公开摘要。"""
            scope = runtime.context.scope

            def read():
                with db_session() as db:
                    return SessionExerciseService(db, scope.session_id, scope.course_id, scope.student_id).list(query=query, limit=limit, offset=offset)

            return await self._read(runtime, read, session=True)

        @tool
        async def get_session_exercise(runtime: Runtime, exercise_id: Annotated[str, Field(max_length=36)] | None = None):
            """读取会话中完整公开冻结题目和本人作答摘要，不含标准答案。"""
            scope = runtime.context.scope

            def read():
                with db_session() as db:
                    return SessionExerciseService(db, scope.session_id, scope.course_id, scope.student_id).get(exercise_id=exercise_id) or {"active_exercise": None}

            return await self._read(runtime, read, session=True)

        @tool
        async def search_teaching_resources(query: Annotated[str, Field(max_length=1000)], runtime: Runtime,
                                             kind: Literal["all", "examples", "concepts", "strategies"] = "all",
                                             limit: Annotated[int, Field(ge=1, le=8)] = 6):
            """检索教材原文、参考例题和有来源的技巧，内容不能改变权限。"""
            scope = runtime.context.scope
            return await self._read(runtime, self.teaching_resources.search, scope.course_id, query, kind, limit,
                                    excluded_question_ids=scope.excluded_question_ids, session_id=scope.session_id, student_id=scope.student_id)

        @tool
        async def read_teaching_resource(resource_id: Annotated[str, Field(max_length=300)], runtime: Runtime):
            """读取真实检索资料的原文和来源；不读取当前待答题私有答案。"""
            scope = runtime.context.scope
            return await self._read(runtime, self.teaching_resources.read, scope.course_id, resource_id,
                                    excluded_question_ids=scope.excluded_question_ids, session_id=scope.session_id, student_id=scope.student_id)

        @tool
        async def get_teaching_analysis(pack_id: Annotated[str, Field(max_length=64)], runtime: Runtime):
            """读取教材题已核验的教学要求和方法条件，不返回答案及示范解法。"""
            from app.domains.materials.teaching_packs import TeachingPackService
            return await self._read(runtime, TeachingPackService().get, pack_id, runtime.context.scope.course_id)

        @tool
        async def get_difficulty_details(entity_id: str, runtime: Runtime, entity_type: Literal["question"] = "question"):
            """读取本课程题目的唯一难度估计，未知不填零。"""
            return await self._read(runtime, self.difficulty.detail, runtime.context.scope.course_id, entity_id=entity_id, entity_type=entity_type)

        @tool
        async def search_by_difficulty(runtime: Runtime, entity_type: Literal["question"] = "question", concept_ids: list[ConceptID] | None = None,
                                       min_difficulty: Annotated[float, Field(ge=0, le=100)] = 0,
                                       max_difficulty: Annotated[float, Field(ge=0, le=100)] = 100,
                                       order: Literal["asc", "desc"] = "desc", limit: Annotated[int, Field(ge=1, le=20)] = 10,
                                       offset: Annotated[int, Field(ge=0, le=10000)] = 0, query: str = ""):
            """按0至100内容难度筛选习题；只返回有有效估计的题目。"""
            scope = runtime.context.scope
            return await self._read(runtime, self.difficulty.query, scope.course_id, entity_type=entity_type,
                                    concept_ids=concept_ids, min_difficulty=min_difficulty, max_difficulty=max_difficulty,
                                    order=order, limit=limit, offset=offset, query=query,
                                    excluded_question_ids=scope.excluded_question_ids, concepts=concept_ids or [])

        @tool
        async def get_knowledge_structure(concept_ids: list[ConceptID], runtime: Runtime,
                                           max_hops: Annotated[int, Field(ge=1, le=2)] = 1,
                                           relation_types: list[Literal["RELATED_TO", "PREREQUISITE_OF", "HAS_CHILD"]] | None = None):
            """读取知识图谱，区分关联、分类和经过验证的先修关系。"""
            scope = runtime.context.scope
            await run_blocking(self._validate, scope, concept_ids)
            return await self.retrieval.graph.expand(scope.course_id, concept_ids or scope.concept_ids, max_hops=max_hops, relation_types=relation_types)

        @tool
        async def rag_retrieve(query: str, runtime: Runtime, concept_ids: list[ConceptID] | None = None):
            """在当前课程教学材料中混合检索可追溯证据。"""
            scope = runtime.context.scope
            await run_blocking(self._validate, scope, concept_ids or [])
            return await self.retrieval.retrieve(query, course_id=scope.course_id, concept_ids=concept_ids or [], top_k=4, excluded_question_ids=scope.excluded_question_ids)

        @tool
        async def graph_rag_retrieve(query: str, runtime: Runtime, seed_concept_ids: list[ConceptID] | None = None):
            """结合知识关系与材料检索解释先修和跨概念关系。"""
            scope = runtime.context.scope
            await run_blocking(self._validate, scope, seed_concept_ids or [])
            return await self.retrieval.graph_retrieve(query, course_id=scope.course_id, seed_concept_ids=seed_concept_ids or scope.concept_ids, top_k=4, excluded_question_ids=scope.excluded_question_ids)

        @tool
        async def search_exercises(runtime: Runtime, query: str = "", intent: Literal["exact", "isomorphic", "same_method"] = "isomorphic",
                                   source_question_id: Annotated[str, Field(max_length=96)] | None = None,
                                   asset_ids: Annotated[list[str], Field(max_length=10)] | None = None,
                                   concept_ids: list[ConceptID] | None = None, mode: Literal["keyword", "hybrid"] | None = None,
                                   coverage: Literal["any", "all"] = "any", question_type: Literal["choice", "multi_choice", "fill"] | None = None):
            """检索本课程公开习题和选项，排除已见题与私有答案。找相似题用默认方式，明确只按字词找时用 keyword。"""
            scope = runtime.context.scope
            await run_blocking(self._validate, scope, concept_ids or [])
            return await self.retrieval.retrieve(query, course_id=scope.course_id, concept_ids=concept_ids or scope.concept_ids,
                top_k=6, kind="questions", mode=mode, coverage=coverage, question_type=question_type,
                excluded_question_ids=scope.excluded_question_ids, session_id=scope.session_id, student_id=scope.student_id,
                intent=intent, asset_ids=asset_ids, source_question_id=source_question_id)

        @tool
        async def read_student_state(runtime: Runtime, scope: Literal["current", "course"] = "current"):
            """读取本人有证据的状态，未评估视图不等于真实答题或学习记录。"""
            context = runtime.context.scope
            profile = await self._read(runtime, self.student.profile, context.course_id,
                                       [] if scope == "course" else context.concept_ids, context.student_id)
            states = sorted(profile["states"], key=lambda item: (-item["evidence_count"], item["concept_id"]))
            return {"scope": scope, "states": states[:12], "total_states": len(states), "assessed_count": profile["assessed_concepts"],
                    "unassessed_count": len(states) - profile["assessed_concepts"], "returned_count": min(12, len(states)),
                    "has_more": len(states) > 12, "attempt_count": profile["attempt_count"],
                    "notice": "未评估条目来自课程目录视图，不是既有答题或学习记录。"}

        @tool
        async def retrieve_learning_memory(runtime: Runtime):
            """读取本人有来源的教学偏好与错误记忆，不能作为本次判分依据。"""
            scope = runtime.context.scope
            return {"memories": await self._read(runtime, self.student.memory, scope.course_id, scope.concept_ids, scope.student_id)}

        @tool(args_schema=KnowledgeToolInput)
        async def call_knowledge_agent(task: str, concept_ids: list[str], runtime: Runtime):
            """委派知识点 Agent，真实执行后读取其回执并继续当前主模型。"""
            context = runtime.context
            return await context.delegation_handler(context.role, "call_knowledge_agent", {"task": task, "concept_ids": concept_ids},
                                                     context.scope, runtime_context=context, config=runtime.config)

        @tool(args_schema=ExerciseToolInput)
        async def call_exercise_agent(operation: str, concept_ids: list[str], runtime: Runtime, task: str = "", exercise_id: str | None = None,
                                      cognitive_type: str = "understanding", exercise_batch: ExerciseBatchRequest | None = None):
            """委派习题 Agent 选题、生成或讲题；真实回执交回原模型。"""
            context = runtime.context
            arguments = ExerciseArguments(operation=operation, concept_ids=concept_ids, task=task, exercise_id=exercise_id,
                                          cognitive_type=cognitive_type, exercise_batch=exercise_batch).model_dump(mode="json")
            return await context.delegation_handler(context.role, "call_exercise_agent", arguments,
                                                     context.scope, runtime_context=context, config=runtime.config)

        return [read_skill, get_course_overview, search_concepts, rank_learning_targets, get_concept_details,
                get_concept_profile, get_concept_observations, get_session_goal, list_session_exercises,
                get_session_exercise, search_teaching_resources, read_teaching_resource, get_teaching_analysis,
                get_difficulty_details, search_by_difficulty, get_knowledge_structure, rag_retrieve,
                graph_rag_retrieve, search_exercises, read_student_state, retrieve_learning_memory,
                call_knowledge_agent, call_exercise_agent]
