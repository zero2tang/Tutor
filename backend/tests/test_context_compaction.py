"""原生摘要保留来源、完整任务和记忆撤销的业务保护。旧缓存控制器测试已退役。"""
from copy import deepcopy


import asyncio


from contextlib import contextmanager


import json


from types import SimpleNamespace


import pytest


from pydantic import BaseModel


from sqlalchemy import create_engine


from sqlalchemy.orm import Session


from app.infrastructure.clients.llm import ModelClient
from app.application.agent.summary import summarize_sources, SummarySourceMismatch


from app.shared.llm_budget import ContextBudgetExceeded, ProviderContextLimitError, RequestBudget, assert_request_budget, estimate_request


from app.agent.context.builder import ContextBuilder, fingerprint


from app.agent.context.policy import CompressionPolicy


from app.agent.context.validation import closed_message_groups


from app.infrastructure.persistence.database import Base


from app.domains.sessions.models import LearningSession


from app.domains.memory.scopes.models import ScopeMemory


from app.domains.memory.scopes.dependencies import ensure_scope_state


from app.domains.memory.context import read_context, validate_manifest


from native_runtime_helpers import LoopState, run_agent_loop


from app.agent.tools.contracts import ToolContext


def settings(**overrides):
    return SimpleNamespace(**{
        "llm_model": "test-model", "context_window_tokens": 32768,
        "context_request_tokens": 14000, "context_safety_tokens": 1000,
        "context_summary_output_tokens": 1200, "context_summary_timeout_seconds": 2,
        **overrides,
    })


class SummaryModel:
    def __init__(self, **options):
        self.settings = settings(**options)
        self.requests = []

    async def chat(self, messages, **kwargs):
        assert_request_budget(self.settings, messages, max_tokens=kwargs["max_tokens"], json_output=True)
        self.requests.append(deepcopy(messages))
        units = json.loads(messages[1]["content"])["committed_closed_history"]
        summary = {
            "topic_segments": ["在当前会话练习含括号方程"],
            "confirmed_findings": ["学生在有帮助条件下完成过尝试，不能据此宣称掌握。"],
            "uncertainties": ["仍需独立作答验证"],
            "unresolved_questions": ["解释负号作用"],
            "source_refs": [unit["source_ref"] for unit in units],
        }
        return {"choices": [{"message": {"content": json.dumps(summary, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50}}


def long_payload():
    return {
        "student_answer": "学生真实长答案" * 80,
        "question": {"id": "q1", "stem": "2(x-3)=8，说明每一步。"},
        "session_goals": {"active_goal_id": "g2", "return_stack": [{"goal_id": "g1", "question_id": "q1"}]},
        "submitted_response_context": {"help_level": 2, "assisted": True},
        "recent_messages": [{"id": str(i), "role": "user" if i % 2 == 0 else "assistant", "content": f"历史{i}：" + "x" * 1000} for i in range(30)],
    }


def test_budget_uses_configured_window_and_output_reserve_separately():
    value = settings(context_request_tokens=32768, context_safety_tokens=2048)
    assert RequestBudget.from_settings(value, 4096).available == 26624
    value.context_input_limit_tokens = 20000
    assert RequestBudget.from_settings(value, 4096).available == 20000
    value.context_request_tokens = 40000
    with pytest.raises(ContextBudgetExceeded):
        RequestBudget.from_settings(value, 4096)


def test_all_request_parts_count_but_base64_size_is_not_image_token_count():
    first = [{"role": "system", "content": "规则"}, {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + "a" * 30}}]}]
    second = deepcopy(first)
    second[1]["content"][0]["image_url"]["url"] += "a" * 100000
    a, b = estimate_request(first, image_tokens=4096), estimate_request(second, image_tokens=4096)
    assert a.tokens == b.tokens and a.image_count == 1 and a.image_tokens == 4096
    assert estimate_request(first, tools=[{"parameters": {"contract": "契约" * 500}}]).tokens > a.tokens
    assert estimate_request(first, json_output=True).tokens > a.tokens
    assert "weighted_unicode" in a.method


def test_builder_preserves_current_answer_question_goals_and_guidance_help():
    payload = long_payload()
    payload["teaching_workspace"] = {
        "pending_question": "为什么这里是负六？", "pending_question_id": "pending-1",
        "help_level": 2, "assisted": True, "problem": payload["question"],
        **{field: [{"raw_input": f"原话{i}"} for i in range(40)] for field in ("evidence", "support_history", "dialogue")},
    }
    saved = deepcopy(payload)
    fixed, units = ContextBuilder().project(payload, "assessment")
    assert payload == saved
    for key in ("student_answer", "question", "session_goals", "submitted_response_context"):
        assert fixed[key] == payload[key]
    assert fixed["teaching_workspace"]["pending_question_id"] == "pending-1"
    assert fixed["teaching_workspace"]["assisted"] is True
    assert all(len(fixed["teaching_workspace"][field]) == 4 for field in ("evidence", "support_history", "dialogue"))
    assert len(units) == 22 + 36 * 3


def test_policy_distinguishes_soft_benefit_and_predictive_mandatory_boundary():
    policy = CompressionPolicy()
    assert policy.reason(7100, 10000, 0, recoverable=500) == "within_budget"
    assert policy.reason(7100, 10000, 0, recoverable=2000) == "soft_boundary"
    assert policy.reason(6000, 10000, 3000) == "preventive_growth"
    assert policy.reason(10001, 10000, 0) == "hard_limit"


def test_tool_calls_and_out_of_order_receipts_form_one_closed_unit():
    calls = [{"id": "a"}, {"id": "b"}]
    messages = [{"role": "assistant", "tool_calls": calls},
                {"role": "tool", "tool_call_id": "b", "content": "B"},
                {"role": "tool", "tool_call_id": "a", "content": "A"}]
    assert closed_message_groups(messages) == [(0, 3)]
    assert closed_message_groups(messages[:-1]) == []
    messages[-1]["tool_call_id"] = "orphan"
    with pytest.raises(ValueError):
        closed_message_groups(messages)


@pytest.mark.asyncio
async def test_final_client_guard_blocks_direct_and_summary_calls_before_transport():
    model = ModelClient.__new__(ModelClient)
    model.settings = settings(context_request_tokens=5000)
    called = []

    async def post(*args, **kwargs):
        called.append(True)

    model._post = post
    with pytest.raises(ContextBudgetExceeded):
        await model.chat([{"role": "user", "content": "学生" * 3000}], max_tokens=1000)
    assert not called


@pytest.mark.asyncio
async def test_compactor_itself_rejects_a_single_unbreakable_oversized_source():
    model = SummaryModel(context_request_tokens=5000)
    with pytest.raises(ContextBudgetExceeded):
        await summarize_sources(model, [{"source_ref": "single", "data": "x" * 20000}])
    assert not model.requests


def test_real_guidance_shape_deduplicates_old_answer_and_same_memory_versions():
    old_answer = "旧轮次完整模拟推理。" * 180
    current_answer = "本次完整模拟推理。" * 190
    student_memory = {"content": "稳定偏好", "version": 3, "source_refs": ["message:m1"]}
    experience = {"session_id": "old-session", "content": "历史经历", "version": 2, "covered_seq": 6}
    payload = {
        "student_message": current_answer,
        "learning_memory": {"student_memory": student_memory, "session_experiences": [experience], "student_memory_version": 3},
        "student_observations": [{"kind": "student_memory", **student_memory}, {"kind": "session_experience", **experience}],
        "submitted_response_context": {"question_id": "pending-1", "help_level": 1, "support_event_ids": ["support-1"]},
        "teaching_workspace": {
            "problem": {"id": "q1", "stem": "4(x-2)=12"}, "pending_question_id": "pending-1",
            "pending_question": "去括号后是什么？", "assisted": True, "help_level": 1,
            "analysis": {"reference_solution": "已有模型推导"}, "analysis_summary": "去括号后求解并检验",
            "dialogue": [{"student_message": old_answer, "content": "上一轮公开反馈"}],
            "evidence": [{"raw_input": old_answer, "response_context": {"assisted": True}}],
            "support_history": [{"id": "support-1", "help_level": 1, "content": "上一轮公开提示"}],
        },
    }
    saved = deepcopy(payload)
    recent, history = ContextBuilder(1).project(payload, "exercise")
    serialized = json.dumps(recent, ensure_ascii=False)
    assert serialized.count(old_answer) == 1 and serialized.count(current_answer) == 1
    assert recent["student_observations"] == []
    assert recent["learning_memory"]["student_memory"] == student_memory
    assert history[0]["kind"] == "guidance.analysis"
    minimal, sources = ContextBuilder(1).project(payload, "exercise", minimal=True)
    assert minimal["student_message"] == current_answer
    assert minimal["submitted_response_context"] == saved["submitted_response_context"]
    assert minimal["teaching_workspace"]["problem"] == saved["teaching_workspace"]["problem"]
    assert minimal["teaching_workspace"]["pending_question_id"] == "pending-1"
    assert minimal["teaching_workspace"]["evidence"] == []
    assert {unit["kind"] for unit in sources} == {"memory.session_experience", "guidance.analysis", "guidance.dialogue", "guidance.evidence", "guidance.support_history"}
    assert payload == saved


def memory_validation_store(tmp_path):
    engine = create_engine("sqlite:///"+(tmp_path/"revocations.db").as_posix())
    Base.metadata.create_all(engine)
    @contextmanager
    def database():
        with Session(engine,expire_on_commit=False) as db:
            yield db
            db.commit()
    with database() as db:
        db.add(LearningSession(id="current",course_id="math",student_id="student",title="当前",goal="方程"))
        ensure_scope_state(db,"student","student")
        ensure_scope_state(db,"student","course","math")
        snapshot = read_context(db,"student","math")
    def guard():
        with database() as db:
            if not validate_manifest(db,snapshot["memory_manifest"]):
                raise ValueError("当前记忆已被纠正或撤销")
    store = SimpleNamespace(validate_memory=guard)
    return store,database,engine


def test_normal_concurrent_memory_updates_allow_frozen_run_snapshot(tmp_path):
    store,database,engine = memory_validation_store(tmp_path)
    with database() as db:
        row = ensure_scope_state(db,"student","student")
        row.version += 1
        row.content = "正常新增正文"
    store.validate_memory()
    engine.dispose()


@pytest.mark.parametrize("kind",["student","course"])
def test_explicit_correction_or_forgetting_blocks_loaded_memory_snapshot(tmp_path,kind):
    store,database,engine = memory_validation_store(tmp_path)
    with database() as db:
        row = ensure_scope_state(db,"student",kind,"math" if kind=="course" else "")
        row.invalidation_version += 1
    with pytest.raises(ValueError,match="纠正或撤销"):
        store.validate_memory()
    engine.dispose()


@pytest.mark.asyncio
async def test_two_layer_summary_uses_short_aliases_and_maps_all_original_sources_back():
    class AliasModel(SummaryModel):
        async def chat(self, messages, **kwargs):
            data = json.loads(messages[1]["content"])
            units = data["committed_closed_history"]
            aliases = [f"s{i}" for i in range(len(units))]
            assert data["allowed_source_refs"] == aliases
            assert [unit["source_ref"] for unit in units] == aliases
            if all(unit["kind"] == "stage" for unit in units):
                assert all("source_refs" not in unit["data"] for unit in units)
            return await super().chat(messages, **kwargs)

    model = AliasModel(context_request_tokens=32768)
    units = [{"source_ref": f"guidance.evidence:{i}:a-long-persistent-source-fingerprint-{i}", "kind": "evidence", "data": {"raw_input": f"真实记录{i}"}} for i in range(35)]
    summary = await summarize_sources(model, units)
    assert len(model.requests) == 3
    assert summary["source_refs"] == [unit["source_ref"] for unit in units]
    assert all(not ref.startswith("s") for ref in summary["source_refs"])


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["missing_stage", "extra_alias", "duplicate_alias"])
async def test_alias_mapping_never_bypasses_missing_extra_or_duplicate_source_coverage(invalid):
    class InvalidAliases(SummaryModel):
        async def chat(self, messages, **kwargs):
            result = await super().chat(messages, **kwargs)
            units = json.loads(messages[1]["content"])["committed_closed_history"]
            if all(unit["kind"] == "stage" for unit in units):
                candidate = json.loads(result["choices"][0]["message"]["content"])
                if invalid == "missing_stage":
                    candidate["source_refs"].pop()
                elif invalid == "extra_alias":
                    candidate["source_refs"].append("s99")
                else:
                    candidate["source_refs"].append("s0")
                result["choices"][0]["message"]["content"] = json.dumps(candidate, ensure_ascii=False)
            return result

    units = [{"source_ref": f"original:{i}", "kind": "evidence", "data": {"value": i}} for i in range(25)]
    with pytest.raises(SummarySourceMismatch):
        await summarize_sources(InvalidAliases(), units)

