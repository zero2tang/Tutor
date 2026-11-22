"""教学运行前准备：隔离评分、读取目标与记忆并校验来源。"""

import asyncio
from copy import deepcopy

from app.agent.runtime.contracts import PreparedRun
from app.shared.llm_budget import RequestBudget
from app.application.agent.prompts import ROLE_NAMES
from app.application.agent.purposes import grading_payload, resolve_purpose
from app.application.agent.streaming import public_fields


class TeachingRunPolicy:
    def __init__(self, models, database, *, memory, instruction_builder, image_factory):
        self.models, self.database = models, database
        self.memory, self.instruction_builder = memory, instruction_builder
        self.image_factory = image_factory

    async def prepare(
        self,
        role,
        skill,
        payload,
        schema,
        context,
        run_id,
        emit,
        use_tools,
        max_tokens,
        *,
        context_purpose=None
    ):
        memory = self.memory()
        citations = []
        purpose = resolve_purpose(role, skill, context_purpose)
        payload = grading_payload(payload) if purpose == "grading" else deepcopy(payload)
        payload["context_purpose"] = purpose
        if purpose == "grading":
            # 正式评分只能依据当前契约与原答，工具也不能回读历史标签。
            use_tools = False
        memory_validator = None
        if context.session_id and purpose != "grading":
            # 各业务入口均补入本会话目标；固定字段不交给历史摘要推断。
            from app.domains.sessions.models import SessionGoalState
            from app.domains.sessions.goals import context_projection

            with self.database() as db:
                # 简化的执行器测试替身不一定实现ORM；真实数据库缺表会正常报错。
                if hasattr(db, "get"):
                    try:
                        goals = db.get(SessionGoalState, context.session_id)
                    except KeyError:
                        goals = None
                    if goals is not None:
                        payload["session_goals"] = context_projection(goals)
        if context.session_id and memory is not None and purpose != "grading":
            business = getattr(memory, "business_pipeline", None)
            if business is not None:
                memory_context = await asyncio.to_thread(
                    memory.context_for_session,
                    context.session_id,
                    context.concept_ids,
                    input_budget=RequestBudget.from_settings(
                        self.models.settings, max_tokens
                    ).available,
                    purpose=purpose,
                )
                manifest = memory_context.pop("memory_manifest", None)
                if manifest is not None:

                    def validate_scoped_memory():
                        if business.hierarchy.validate_context(manifest) is False:
                            raise ValueError("当前记忆已被纠正或撤销，请重新读取会话。")

                    memory_validator = validate_scoped_memory
                # 完整快照供API使用；模型只接收预算后的投影，避免同一正文重复注入。
                projected = memory_context.pop("model_payload", None)
                if projected is not None:
                    memory_context = {
                        **projected,
                        "memory_invalidation_fingerprint": memory_context.get(
                            "memory_invalidation_fingerprint", ""
                        ),
                        "memory_input_tokens": memory_context.get("memory_input_tokens", 0),
                        "memory_budget": memory_context.get("memory_budget", 0),
                    }
                memory_context["schema_version"] = "business-memory-v2"
            else:
                memory_context = await asyncio.to_thread(
                    memory.context_for_session,
                    context.session_id,
                    context.concept_ids,
                )
            # 撤销由实时manifest校验执行；管理列表不注入模型。
            memory_context.pop("invalidated_source_refs", None)
            payload["learning_memory"] = memory_context
            if emit:
                await emit(
                    "memory_context_loaded",
                    {
                        "run_id": run_id,
                        "agent": role,
                        "context_purpose": purpose,
                        "memory_schema": memory_context.get("schema_version", "legacy"),
                        "scope_receipts": [
                            {
                                key: item.get(key)
                                for key in ("scope_key", "scope_kind", "version", "concept_id")
                            }
                            for item in [
                                memory_context.get("global_memory"),
                                memory_context.get("course_memory"),
                                *memory_context.get("concept_memories", []),
                            ]
                            if isinstance(item, dict)
                        ],
                        "student_memory_version": memory_context.get("student_memory_version", 0),
                        "student_memory_present": bool(memory_context.get("student_memory")),
                        "session_experience_ids": [
                            item["session_id"]
                            for item in memory_context.get("session_experiences", [])
                        ],
                        "session_experience_versions": [
                            item["version"]
                            for item in memory_context.get("session_experiences", [])
                        ],
                    },
                )
        instruction = self.instruction_builder(role, schema, skill)
        if purpose == "grading":
            instruction += "\n本次是隔离的正式评分：仅依据当前题、原答、评分要点和提交前帮助条件。没有历史能力标签；不得补造历史表现。逐概念没有可定位依据时必须标记not_observed。"
        if emit:
            await emit(
                "context_purpose_selected",
                {
                    "run_id": run_id,
                    "agent": role,
                    "context_purpose": purpose,
                    "history_memory_allowed": purpose != "grading",
                    "tools_enabled": use_tools,
                },
            )
        return PreparedRun(
            payload=payload,
            instruction=instruction,
            purpose=purpose,
            use_tools=use_tools,
            display_name=ROLE_NAMES[role],
            public_fields=public_fields(schema),
            citations=citations,
            memory_validator=memory_validator,
            image_context=self.image_factory(context.course_id),
            preloaded_skills=(
                frozenset({skill}) if skill and skill != "auto" else frozenset()
            ),
        )

