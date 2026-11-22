"""以真实业务服务验证通用教学状态、工具引用来源和原始学生证据。"""

from copy import deepcopy
from datetime import datetime
import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine, func, select

from app.infrastructure.persistence import database
from app.infrastructure.persistence.database import Base, db_session
from app.domains.guidance.schemas import GuidanceRequest
from app.domains.courses.models import Concept, Course, Material, Question
from app.domains.sessions.models import LearningSession, Message
from app.domains.students.models import StudentState
from app.domains.learning.schemas import MessageRequest
from app.application.learning.service import LearningService
from app.application.learning.receipts import receive
from app.domains.sessions.service import SessionService
from app.application.agent.tools import TeachingTools
from langgraph.checkpoint.memory import InMemorySaver
from native_tools import invoke_tool
from app.domains.courses.bootstrap import seed_course

PRIVATE_REFERENCE = "PRIVATE_REFERENCE_DO_NOT_PUBLISH:3x+6=42,x=12"
OWN_RESOURCE = "teach:material:junior_algebra:fixture"
FOREIGN_RESOURCE = "teach:material:mechanics:fixture"
EXAMPLE_RESOURCE = "teach:question:junior_algebra:worked-example"
QUANTITY_CONCEPT = "junior_algebra:fixture-quantity"


def output(**changes):
    value = {
        "action": "ask",
        "content": "先找出总价中哪些费用会随本数变化，再决定怎样表示。",
        "question": "如果购买x本，书本费用怎样表示？",
        "focus": "数量关系",
        "help_level": 0,
        "analysis": {
            "summary": "区分固定费用和随本数变化的费用。",
            "foundations": ["乘法表示数量关系"],
            "techniques": [],
            "solution_paths": [],
            "related_resources": [],
            "reference_solution": PRIVATE_REFERENCE,
            "uncertainties": [],
            "source_ids": [],
        },
        "observation": "尚未获得学生作答。",
        "understanding": "unknown",
        "student_input_kind": "other",
        "source_ids": [],
        "transfer_problem": None,
    }
    value.update(changes)
    return value


async def native_tool_result(tools, role, name, arguments, scope):
    # 测试只替换模型候选，仍执行真实工具函数及资料来源观察器。
    return await invoke_tool(tools, role, name, arguments, scope), {}


async def run_guidance(learning, sid, request, turn_id, emit):
    # 领域用例明确持有执行权；公开HTTP请求另由LearningService.execute统一领取。
    learning.claim(sid, turn_id)
    failed = True
    try:
        with db_session() as db:
            receive(db, sid, turn_id, {"kind": "guidance_action", **request.model_dump()})
        result = await learning.guidance.act_owned(sid, request, turn_id, emit)
        learning.finish(sid, turn_id, {"kind": "guidance_action", **request.model_dump()}, result, [result["message"]["id"]])
        failed = False
        return result
    finally:
        learning.release(sid, turn_id, failed=failed)


class TeachingRunner:
    """只替换模型输出；工具读取、会话事务及权限校验仍走真实实现。"""

    def __init__(self):
        self.calls = []
        self.responses = []
        self.read_resource = None
        self.read_concept = None
        self.search_query = None
        self.search_results = []
        self.forged_citations = []
        self.fail = False
        self.tools = TeachingTools(None)

    async def run(self, role, skill, payload, schema, context, turn_id, **kwargs):
        self.calls.append(
            {
                "role": role,
                "skill": skill,
                "payload": deepcopy(payload),
                "context": deepcopy(context),
            }
        )
        assert role == "exercise" and skill == "guided_explanation"
        assert kwargs.get("use_tools", True)
        if self.fail:
            raise RuntimeError("模拟模型调用失败，不伪造教学结果。")
        value = deepcopy(self.responses.pop(0) if self.responses else output())
        if self.search_query:
            result, _ = await native_tool_result(kwargs["tool_source"],
                "exercise",
                "search_teaching_resources",
                {"query": self.search_query, "kind": "examples", "limit": 6},
                context,
            )
            self.search_results = result["items"]
        if self.read_resource:
            result, _ = await native_tool_result(kwargs["tool_source"],
                "exercise",
                "read_teaching_resource",
                {"resource_id": self.read_resource},
                context,
            )
            ids = [item["id"] for item in result.get("citations", [])]
            value["source_ids"] = ids
            if value.get("analysis"):
                value["analysis"]["source_ids"] = ids
        if self.read_concept:
            result, _ = await native_tool_result(kwargs["tool_source"],
                "exercise",
                "get_concept_details",
                {"concept_id": self.read_concept},
                context,
            )
            ids = [item["id"] for item in result["citations"]]
            value["source_ids"] = ids
            if value.get("analysis"):
                value["analysis"]["source_ids"] = ids
        value = schema.model_validate(value).model_dump()
        if kwargs.get("validate_output"):
            kwargs["validate_output"](value)
        return {
            "output": value,
            "citations": deepcopy(self.forged_citations),
            "run_id": str(uuid4()),
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            "duration_ms": 3.5,
        }


@pytest.fixture
def teaching(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + (tmp_path / "guided.db").as_posix())
    Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "engine", lambda: engine)
    seed_course()
    with db_session() as db:
        db.add(
            Course(
                id="mechanics",
                title="力学测试课程",
                description="自编隔离测试资料",
                source_ref="fixture://mechanics",
            )
        )
        db.add(
            Concept(
                id=QUANTITY_CONCEPT,
                course_id="junior_algebra",
                source_id="fixture-quantity",
                name="自编数量关系概念",
                summary="总费用由固定费用和随数量变化的费用组成。",
                topic_type="procedure",
                module="自编测试资料",
            )
        )
        for course in ("junior_algebra", "mechanics"):
            db.add(
                Material(
                    id=course + ":fixture",
                    course_id=course,
                    title=(
                        "自编固定费用规则"
                        if course == "junior_algebra"
                        else "自编路程与位移定义"
                    ),
                    text=(
                        "总价是固定费用加上单价与数量的乘积。"
                        if course == "junior_algebra"
                        else "路程是路径长度；位移由起点终点确定。"
                    ),
                    concept_ids=(
                        [QUANTITY_CONCEPT] if course == "junior_algebra" else []
                    ),
                    source_ref="fixture://self_authored/" + course,
                    origin="self_authored",
                    visibility="public",
                )
            )
        db.add(
            Question(
                id="junior_algebra:worked-example",
                course_id="junior_algebra",
                source_id="self_authored_example",
                stem="【自编测试例题】每支笔2元，配送费8元，共28元，买了多少支？",
                options={},
                answer=["10"],
                question_type="fill",
                cognitive_type="application",
                concept_ids=[],
                explanation="先从总价扣除固定配送费8元，再除以单价2元，得到10支。",
                source_ref="fixture://self_authored/worked-example",
                validation={"availability": "ready", "data_origin": "self_authored"},
            )
        )
    runner = TeachingRunner()
    learning = LearningService(runner, checkpointer=InMemorySaver())
    session = learning.sessions.create(
        "自编新题引导", [], "junior_algebra", "evaluation_123456abcdef"
    )
    yield learning, runner, session["id"]
    engine.dispose()


async def submit(learning, sid, action, text="", **extra):
    events = []

    async def emit(name, data):
        events.append((name, data))

    body = GuidanceRequest(action=action, text=text, request_id=str(uuid4()), **extra)
    result = await run_guidance(learning,sid, body, str(uuid4()), emit)
    return result, body, events


async def start(learning, sid):
    return await submit(
        learning,
        sid,
        "start",
        problem={
            "stem": "练习本每本3元，运费6元，共42元，买了多少本？",
            "source_kind": "recognized",
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("old_input", [False, True])
async def test_owned_guidance_rejects_missing_native_input_without_creating_another_raw_message(teaching, old_input):
    learning, runner, sid = teaching
    request = GuidanceRequest(action="start", problem={"stem": "每本3元，运费6元，共42元，买了几本？"},
        request_id=str(uuid4()))
    if old_input:
        with db_session() as db:
            db.add(Message(id=str(uuid4()), session_id=sid, role="user", content="历史原话保留。", citations=[],
                metadata_json={"guidance_request_id": request.request_id, "_tutoring_order": [1, 0]}))
    learning.claim(sid, "requires-native")
    try:
        async def emit(*args):
            pass
        with pytest.raises(ValueError, match="统一学习入口"):
            await learning.guidance.act_owned(sid, request, "requires-native", emit)
    finally:
        learning.release(sid, "requires-native", failed=True)
    assert not runner.calls
    with db_session() as db:
        rows = list(db.scalars(select(Message).where(Message.session_id == sid, Message.role == "user")))
        assert len(rows) == int(old_input)
        assert not any(row.metadata_json.get("_native_request") for row in rows)


@pytest.mark.asyncio
async def test_same_second_guidance_messages_restore_in_real_input_response_order(teaching):
    learning, _, sid = teaching

    async def emit(*args):
        pass

    first = await learning.execute(sid, {"kind": "guidance_action", "action": "start",
        "problem": {"stem": "每本3元，运费6元，共42元，买了几本？"},
        "request_id": str(uuid4())}, "ordered-first", emit)
    for index, text in enumerate(("我先找出固定运费。", "然后考虑本数变化引起的费用。"), 2):
        first = await learning.turn(sid, MessageRequest(content=text, request_id=str(uuid4()),
            wait_id=first["session"]["pending_interaction"]["wait_id"]), f"ordered-{index}", emit)
    with db_session() as db:
        for row in db.scalars(select(Message).where(Message.session_id == sid)):
            row.created_at = datetime(2026, 1, 1, 12, 0, 0)
    restored = learning.sessions.get(sid)["messages"]
    assert [message["role"] for message in restored] == ["user", "assistant"] * 3
    assert restored[2]["content"] == "我先找出固定运费。"
    assert restored[4]["content"] == "然后考虑本数变化引起的费用。"


def raw_workspace(sid):
    with db_session() as db:
        from app.domains.guidance.history import hydrate
        return hydrate(db,deepcopy(db.get(LearningSession, sid).pending_context["guided_teaching"]))


def assert_public(value):
    text = json.dumps(value, ensure_ascii=False)
    assert PRIVATE_REFERENCE not in text
    assert "reference_solution" not in text


@pytest.mark.asyncio
async def test_skip_uses_workspace_state_and_preserves_archived_tutoring(teaching):
    learning, _, sid = teaching
    archived = {"status": "archived", "stem": "3x+6=42", "question_id": "old-question", "notice": "历史题"}
    with db_session() as db:
        row = db.get(LearningSession, sid)
        row.pending_context = {"archived_problem": deepcopy(archived)}
    # 没有真实待办的旧页面不能猜测一个停点执行跳过；历史学习档案仍须保留。
    async def emit(*_):
        pass

    with pytest.raises(ValueError, match="等待点"):
        await learning.execute(sid, {"kind": "skip", "request_id": str(uuid4()),
            "wait_id": "expired"}, str(uuid4()), emit)
    with db_session() as db:
        assert (
            db.get(LearningSession, sid).pending_context["archived_problem"] == archived
        )
    await start(learning, sid)
    before = raw_workspace(sid)
    with pytest.raises(ValueError, match="讲题工作区"):
        await learning.execute(sid, {"kind": "skip", "request_id": str(uuid4()),
            "wait_id": learning.sessions.get(sid)["pending_interaction"]["wait_id"]}, str(uuid4()), emit)
    assert raw_workspace(sid) == before


@pytest.mark.asyncio
async def test_first_analysis_persists_privately_and_new_service_restores_public_state(
    teaching,
):
    learning, runner, sid = teaching
    result, _, events = await start(learning, sid)
    assert result["guidance"]["pending_question"]
    assert result["guidance"]["turn_count"] == 1
    assert_public(result["guidance"])
    assert raw_workspace(sid)["analysis"]["reference_solution"] == PRIVATE_REFERENCE
    restored = LearningService(TeachingRunner()).sessions.get(sid)
    assert restored["guidance"] == result["guidance"]
    assert restored["active_turn_id"] is None
    assert_public(restored)
    assert any(name == "message" for name, _ in events)
    assert runner.calls[0]["context"].course_id == "junior_algebra"


@pytest.mark.asyncio
async def test_same_request_replays_without_model_or_duplicate_raw_messages(teaching):
    learning, runner, sid = teaching
    first, request, _ = await start(learning, sid)
    calls = len(runner.calls)

    replay_events = []

    async def no_emit(name, value):
        # SSE 重连可以重放同一正式消息，禁止的是新写入或再次执行模型。
        replay_events.append((name, value))

    replay = await run_guidance(learning,sid, request, str(uuid4()), no_emit)
    assert replay["replayed"]
    assert replay["message"]["id"] == first["message"]["id"]
    assert not any(name.startswith("agent_") for name, _ in replay_events)
    assert len(runner.calls) == calls
    with db_session() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.session_id == sid)
            )
            == 2
        )
    with pytest.raises(ValueError):
        await run_guidance(learning,
            sid,
            request.model_copy(update={"text": "相同请求号下不同文字"}),
            str(uuid4()),
            no_emit,
        )
    assert len(runner.calls) == calls


@pytest.mark.asyncio
async def test_stale_version_cannot_call_model_or_replace_the_workspace(teaching):
    learning, runner, sid = teaching
    await start(learning, sid)
    before = raw_workspace(sid)
    calls = len(runner.calls)
    with pytest.raises(ValueError):
        await submit(learning, sid, "reply", "旧页面学生回答", expected_version=0)
    assert raw_workspace(sid) == before
    assert len(runner.calls) == calls


@pytest.mark.asyncio
async def test_model_failure_preserves_original_input_and_retry_does_not_duplicate_it(
    teaching,
):
    learning, runner, sid = teaching
    runner.fail = True
    request = GuidanceRequest(
        action="start",
        problem={
            "stem": "每本3元，运费6元，一共42元，买了多少本？",
            "source_kind": "recognized",
        },
        request_id=str(uuid4()),
    )

    async def emit(*_):
        pass

    with pytest.raises(RuntimeError, match="模拟模型"):
        await run_guidance(learning,sid, request, str(uuid4()), emit)
    snapshot = learning.sessions.get(sid)
    assert snapshot["active_turn_id"] is None
    assert snapshot.get("guidance") is None
    with db_session() as db:
        messages = list(db.scalars(select(Message).where(Message.session_id == sid)))
        assert len(messages) == 1 and messages[0].role == "user"
        assert messages[0].content == request.problem.stem
    runner.fail = False
    result = await run_guidance(learning,sid, request, str(uuid4()), emit)
    assert result["guidance"]["turn_count"] == 1
    with db_session() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.session_id == sid, Message.role == "user")
            )
            == 1
        )


@pytest.mark.asyncio
async def test_followup_and_answer_routes_pass_student_words_and_private_reference_to_same_agent(
    teaching,
):
    learning, runner, sid = teaching
    await start(learning, sid)
    text = "为什么运费要单独减掉？我不明白两边同时减6的道理。"

    async def emit(*_):
        pass

    result = await learning.turn(
        sid, MessageRequest(content=text, request_id=str(uuid4()),
            wait_id=learning.sessions.get(sid)["pending_interaction"]["wait_id"]), str(uuid4()), emit
    )
    assert runner.calls[-1]["payload"]["student_message"] == text
    assert (
        runner.calls[-1]["payload"]["teaching_workspace"]["analysis"][
            "reference_solution"
        ]
        == PRIVATE_REFERENCE
    )
    assert result["guidance"]["turn_count"] == 2
    answer = "我试着用3x表示书本费用，再加6。"
    result = await learning.execute(sid, {"kind": "guidance_action", "action": "reply", "text": answer,
        "request_id": str(uuid4()), "wait_id": learning.sessions.get(sid)["pending_interaction"]["wait_id"]},
        str(uuid4()), emit)
    assert runner.calls[-1]["payload"]["student_message"] == answer
    assert result["guidance"]["turn_count"] == 3
    assert raw_workspace(sid)["evidence"][-1]["raw_input"] == answer
    with db_session() as db:
        assert text in [
            message.content
            for message in db.scalars(
                select(Message).where(Message.session_id == sid, Message.role == "user")
            )
        ]


@pytest.mark.asyncio
async def test_only_fact_sources_returned_by_real_read_tools_can_be_cited(teaching):
    learning, runner, sid = teaching
    runner.read_resource = OWN_RESOURCE
    runner.forged_citations = [
        {
            "id": "invented-by-model",
            "title": "不存在的资料",
            "course_id": "junior_algebra",
        }
    ]
    result, _, _ = await start(learning, sid)
    sources = result["guidance"]["sources"]
    assert {item["id"] for item in sources} == {OWN_RESOURCE}
    assert all(item["course_id"] == "junior_algebra" for item in sources)
    assert {item["id"] for item in result["message"]["citations"]} == {OWN_RESOURCE}
    assert "invented-by-model" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.asyncio
async def test_fabricated_source_ids_fail_before_any_teaching_result_is_committed(
    teaching,
):
    learning, runner, sid = teaching
    runner.responses = [output(source_ids=["not-returned-by-tools"])]
    with pytest.raises(ValueError):
        await start(learning, sid)
    current = learning.sessions.get(sid)
    assert current.get("guidance") is None
    assert current["active_turn_id"] is None


@pytest.mark.asyncio
async def test_cross_course_material_read_is_rejected_by_real_tool_scope(teaching):
    learning, runner, sid = teaching
    runner.read_resource = FOREIGN_RESOURCE
    with pytest.raises(PermissionError):
        await start(learning, sid)
    assert not learning.sessions.get(sid).get("guidance")
    assert learning.sessions.get(sid)["active_turn_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "course,stem",
    [
        ("junior_algebra", "练习本每本3元，运费6元，共42元，买了多少本？"),
        ("mechanics", "沿400米跑道走一圈回到起点，路程与位移为何不同？"),
    ],
)
async def test_new_problem_teaching_never_uses_equation_rule_validator(
    teaching, monkeypatch, course, stem
):
    learning, _, sid = teaching
    if course != "junior_algebra":
        sid = learning.sessions.create(
            "概念性新题", [], course, "evaluation_123456abcdef"
        )["id"]

    import sys

    # 旧模块不可导入时，通用讲题仍应正常运行。
    monkeypatch.setitem(sys.modules, "app.tutoring", None)
    monkeypatch.setitem(sys.modules, "app.teaching_analysis", None)
    result, _, _ = await submit(
        learning, sid, "start", problem={"stem": stem, "source_kind": "pasted"}
    )
    assert result["guidance"]["pending_question"]


@pytest.mark.asyncio
async def test_assisted_original_and_independent_transfer_remain_raw_evidence_not_mastery(
    teaching,
):
    learning, runner, sid = teaching
    await start(learning, sid)
    runner.responses = [output(action="hint", help_level=2, understanding="needs_help")]
    result, _, _ = await submit(learning, sid, "hint", "请帮我列出数量关系。")
    assert result["guidance"]["assisted"]
    runner.responses = [
        output(
            action="transfer",
            analysis=None,
            content="现在换一个新情境，请独立思考，不先查看解答。",
            question="每张票的费用怎样表示？",
            transfer_problem={
                "stem": "若干张门票，每张5元，另付10元手续费，总共45元，买了多少张？",
                "reference_solution": "5n+10=45，n=7",
                "target": "独立建立固定费用模型",
            },
        )
    ]
    await submit(learning, sid, "transfer")
    runner.responses = [
        output(
            action="complete",
            analysis=None,
            question="",
            understanding="demonstrated",
            student_input_kind="attempt",
            observation="学生写出5n+10=45并解得n=7，属于本次模型观察。",
        )
    ]
    result, _, _ = await submit(
        learning, sid, "reply", "5n+10=45，因此n=7。我独立完成。"
    )
    state = raw_workspace(sid)
    last = state["evidence"][-1]
    assert last["transfer_response"] and last["independent_response"]
    assert last["raw_input"] == "5n+10=45，因此n=7。我独立完成。"
    assert last["model_understanding"] == "demonstrated"
    assert last["assessment_method"] == "model_observation_not_program_proof"
    assert state["assisted"]
    assert "mastery" not in result["guidance"]
    with db_session() as db:
        assert db.scalar(select(func.count()).select_from(StudentState)) == 0


@pytest.mark.asyncio
async def test_guidance_http_sse_publishes_public_state_and_can_restore(teaching):
    from fastapi import FastAPI
    from app.api.routes.guidance import router

    learning, _, sid = teaching
    app = FastAPI()
    app.include_router(router)
    app.state.services = {"learning": learning, "sessions": learning.sessions}
    app.state.tasks = set()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/sessions/{sid}/guidance?stream=true",
            json={
                "action": "start",
                "problem": {
                    "stem": "每本3元，另付运费6元，共42元，问购买几本？",
                    "source_kind": "recognized",
                },
                "request_id": str(uuid4()),
            },
        )
    assert response.status_code == 200
    assert "event: message" in response.text and "event: guidance" in response.text
    assert "event: done" in response.text
    assert PRIVATE_REFERENCE not in response.text
    assert learning.sessions.get(sid)["guidance"]["turn_count"] == 1


@pytest.mark.asyncio
async def test_close_then_new_problem_archives_private_workspace_and_remembers_seen_text(
    teaching,
):
    learning, runner, sid = teaching
    first, _, _ = await start(learning, sid)
    original_stem = first["guidance"]["problem"]["stem"]
    original_id = first["guidance"]["id"]
    await submit(learning, sid, "reply", "我认为固定运费不随着本数变化。")
    closed, _, _ = await submit(learning, sid, "close")
    assert closed["guidance"]["closed"]
    assert not learning.guidance.active(learning.sessions.get(sid))
    new_stem = "若干本词典每本8元，加上12元配送费，共68元，买了几本？"
    second, _, _ = await submit(
        learning, sid, "start", problem={"stem": new_stem, "source_kind": "pasted"}
    )
    assert second["guidance"]["id"] != original_id
    with db_session() as db:
        messages=list(db.scalars(select(Message).where(Message.session_id==sid)))
        assert not any("_previous_guidance_workspace" in m.metadata_json for m in messages)
        from app.domains.guidance.history import hydrate
        original_refs=[m.id for m in messages if m.metadata_json.get("workspace_id")==original_id]
        archived=hydrate(db,{"id":original_id,"session_id":sid,"history_refs":original_refs})
    assert archived["analysis"]["reference_solution"] == PRIVATE_REFERENCE
    assert archived["evidence"][-2]["raw_input"] == "我认为固定运费不随着本数变化。"
    assert_public(learning.sessions.get(sid))
    before = raw_workspace(sid)
    # 文本哈希去掉空白并规范全角数字，不能把原题重新包装成独立验证题。
    runner.responses = [
        output(
            action="transfer",
            analysis=None,
            transfer_problem={
                "stem": "  " + original_stem.replace("3", "３") + "\n",
                "reference_solution": "旧题参考解",
                "target": "重复原题不能作为独立新题",
            },
        )
    ]
    with pytest.raises(ValueError, match="已在工作区出现"):
        await submit(learning, sid, "transfer")
    assert raw_workspace(sid) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "original_help,followup_request,followup_teaching",
    [("hint", "reply", "explain"), ("explain", "hint", "hint")],
)
async def test_help_after_independent_transfer_response_contaminates_later_verification(
    teaching, original_help, followup_request, followup_teaching
):
    learning, runner, sid = teaching
    await start(learning, sid)
    runner.responses = [
        output(
            action="worked_example" if original_help == "explain" else "hint",
            help_level=3 if original_help == "explain" else 1,
        )
    ]
    result, _, _ = await submit(learning, sid, original_help, "需要帮助理解原题。")
    assert result["guidance"]["assisted"]
    runner.responses = [
        output(
            action="transfer",
            analysis=None,
            transfer_problem={
                "stem": "新题：每袋苹果6元，配送费9元，共39元，买了几袋？",
                "reference_solution": "6n+9=39，因此n=5",
                "target": "撤去帮助后建立数量关系",
            },
        )
    ]
    await submit(learning, sid, "transfer")
    runner.responses = [
        output(
            action="ask",
            analysis=None,
            understanding="partial",
            student_input_kind="attempt",
            observation="学生写出6n+9=39，尚未给出后续求解结果。",
        )
    ]
    await submit(learning, sid, "reply", "我先列6n+9=39。")
    assert raw_workspace(sid)["evidence"][-1]["independent_response"]
    runner.responses = [
        output(
            action=followup_teaching,
            analysis=None,
            help_level=0,
            understanding="needs_help",
        )
    ]
    await submit(
        learning, sid, followup_request, "为什么两边减去9仍然相等？我需要讲清这个原因。"
    )
    after_help = raw_workspace(sid)
    assert after_help["transfer"]["assisted"]
    assert not after_help["evidence"][-1]["independent_response"]
    runner.responses = [
        output(
            action="complete",
            analysis=None,
            question="",
            understanding="demonstrated",
            student_input_kind="attempt",
        )
    ]
    await submit(learning, sid, "reply", "现在理解了，所以n=5。")
    evidence = raw_workspace(sid)["evidence"][-1]
    assert evidence["transfer_response"] and not evidence["independent_response"]
    assert evidence["model_understanding"] == "demonstrated"
    with db_session() as db:
        assert db.scalar(select(func.count()).select_from(StudentState)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "为什么要这样列式？请详细解释固定费用。",
        "我不是要完整答案，只想追问这一个量为什么不随本数变化。",
    ],
)
async def test_message_explain_intent_still_delivers_free_followup_to_active_guidance(
    teaching, text
):
    learning, runner, sid = teaching
    await start(learning, sid)

    async def emit(*_):
        pass

    await learning.turn(
        sid,
        MessageRequest(content=text, intent="explain", request_id=str(uuid4()),
            wait_id=learning.sessions.get(sid)["pending_interaction"]["wait_id"]),
        str(uuid4()),
        emit,
    )
    payload = runner.calls[-1]["payload"]
    assert payload["requested_action"] == "reply"
    assert payload["student_message"] == text
    assert raw_workspace(sid)["evidence"][-1]["raw_input"] == text


@pytest.mark.asyncio
async def test_old_equation_endpoint_cannot_take_over_active_guidance(teaching):
    from app.main import app

    learning, runner, sid = teaching
    await start(learning, sid)
    before = raw_workspace(sid)
    calls = len(runner.calls)

    assert not hasattr(learning, "equations")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/sessions/{sid}/tutoring",
            json={"action": "start", "request_id": str(uuid4())},
        )
    assert response.status_code == 404
    assert raw_workspace(sid) == before
    with db_session() as db:
        assert "equation_tutoring" not in db.get(LearningSession, sid).pending_context
    assert len(runner.calls) == calls


@pytest.mark.asyncio
async def test_legacy_session_resumes_general_guidance_without_changing_history(
    teaching,
):
    learning, runner, sid = teaching
    legacy = {
        "status": "working",
        "current_equation": "3x=36",
        "accepted_steps": ["3x=36"],
        "assisted": True,
        "diagnoses": [{"observation": "历史局部观察"}],
        "contract": {
            "id": "old-question",
            "equation": "3x+6=42",
            "answer": "PRIVATE_OLD_ANSWER",
        },
    }
    with db_session() as db:
        db.get(LearningSession, sid).pending_context = {"archived_problem": {
            "question_id": "old-question", "stem": legacy["contract"]["equation"],
            "status": "archived", "notice": "原题已归档", "answer": "PRIVATE_OLD_ANSWER"}}
    restored = learning.sessions.get(sid)
    assert restored["archived_problem"]["status"] == "archived"
    assert "tutoring" not in restored
    assert "PRIVATE_OLD_ANSWER" not in json.dumps(restored)

    async def emit(*_):
        pass

    from app.domains.guidance.schemas import ExternalProblem, GuidanceRequest
    result = await run_guidance(learning,sid, GuidanceRequest(action="start",
        problem=ExternalProblem(stem=restored["archived_problem"]["stem"]),
        text="为什么两边同时减6？", request_id=str(uuid4())), str(uuid4()), emit)
    assert result["guidance"]["problem"]["stem"] == "3x+6=42"
    assert runner.calls[-1]["payload"]["student_message"] == "为什么两边同时减6？"
    with db_session() as db:
        context = db.get(LearningSession, sid).pending_context
        assert "equation_tutoring" not in context
        assert "legacy_history" not in context["guided_teaching"]


@pytest.mark.asyncio
async def test_textbook_question_without_answer_starts_guidance(teaching):
    learning, runner, sid = teaching
    stem = "比较两种分类方案，说明为什么可能重复计数。"
    with db_session() as db:
        db.add(
            Question(
                id="junior_algebra:source-only",
                course_id="junior_algebra",
                source_id="self_authored",
                stem=stem,
                options={},
                answer=[],
                question_type="open",
                concept_ids=[],
                explanation="",
                source_ref="fixture://source-only",
                validation={
                    "teaching_pack_id": "archived-pack",
                    "availability": "reference_only",
                },
            )
        )

    async def emit(*_):
        pass

    result = await learning.execute(sid, {"kind": "practice_existing", "question_id": "junior_algebra:source-only",
        "cognitive_type": "understanding", "request_id": str(uuid4())}, str(uuid4()), emit)
    assert result["guidance"]["problem"]["stem"] == stem
    assert runner.calls[-1]["skill"] == "guided_explanation"
    with db_session() as db:
        assert db.get(Question, "junior_algebra:source-only").answer == []
        assert db.get(LearningSession, sid).pending_question_id is None


@pytest.mark.asyncio
async def test_empty_junior_session_uses_main_agent_instead_of_default_equation(
    teaching, monkeypatch
):
    learning, runner, sid = teaching

    async def orchestrate(*_args, **_kwargs):
        return {"route": "main_agent"}

    async def emit(*_):
        pass

    monkeypatch.setattr(learning, "orchestrate", orchestrate)
    async def publish(_ctx, _event, proposal, _assessment):
        return proposal
    monkeypatch.setattr(learning, "publish_proposal", publish)
    result = await learning.turn(
        sid,
        MessageRequest(content="我想理解等式性质。", request_id=str(uuid4())),
        str(uuid4()),
        emit,
    )
    assert result["route"] == "main_agent"
    assert not hasattr(learning, "equations")
    assert not runner.calls


@pytest.mark.asyncio
async def test_closed_workspace_rejects_direct_reply_without_resurrecting_it(teaching):
    learning, runner, sid = teaching
    await start(learning, sid)
    await submit(learning, sid, "close")
    before = raw_workspace(sid)
    calls = len(runner.calls)
    with pytest.raises(ValueError):
        await submit(learning, sid, "reply", "结束后旧页面继续提交，不能复活原题。")
    assert raw_workspace(sid) == before
    assert len(runner.calls) == calls
    assert learning.sessions.get(sid)["active_turn_id"] is None


@pytest.mark.asyncio
async def test_concurrent_receipt_committed_between_replay_check_and_claim_is_replayed(
    teaching, monkeypatch
):
    learning, runner, sid = teaching
    await start(learning, sid)
    with db_session() as db:
        row = db.get(LearningSession, sid)
        prior_context, prior_version = deepcopy(row.pending_context), row.version
        prior_message_ids = {
            item.id
            for item in db.scalars(select(Message).where(Message.session_id == sid))
        }
    request = GuidanceRequest(
        action="reply", text="我先设购买x本，书本费用为3x。", request_id=str(uuid4())
    )

    async def emit(*_):
        pass

    winner = await run_guidance(learning,sid, request, str(uuid4()), emit)
    with db_session() as db:
        row = db.get(LearningSession, sid)
        winning_context, winning_version = deepcopy(row.pending_context), row.version
        winning_messages = [
            {
                "id": item.id,
                "session_id": item.session_id,
                "role": item.role,
                "agent": item.agent,
                "content": item.content,
                "citations": deepcopy(item.citations),
                "metadata_json": deepcopy(item.metadata_json),
                "created_at": item.created_at,
            }
            for item in db.scalars(select(Message).where(Message.session_id == sid))
            if item.id not in prior_message_ids
        ]
        for item in list(db.scalars(select(Message).where(Message.session_id == sid))):
            if item.id not in prior_message_ids:
                db.delete(item)
        row.pending_context, row.version = prior_context, prior_version
    original_claim = learning.leases.claim
    injected = False

    def race_claim(session_id, turn_id, **kwargs):
        nonlocal injected
        assert not injected
        injected = True
        # 模拟另一个工作进程恰好在初次replay检查后提交，随后本进程取得占用。
        with db_session() as db:
            row = db.get(LearningSession, session_id)
            row.pending_context, row.version = (
                deepcopy(winning_context),
                winning_version,
            )
            for values in winning_messages:
                db.add(Message(**deepcopy(values)))
        return original_claim(session_id, turn_id, **kwargs)

    monkeypatch.setattr(learning.leases, "claim", race_claim)
    calls = len(runner.calls)
    replay = await run_guidance(learning,sid, request, str(uuid4()), emit)
    assert injected and replay["replayed"]
    assert replay["message"]["id"] == winner["message"]["id"]
    assert len(runner.calls) == calls
    with db_session() as db:
        assert db.get(LearningSession,sid).pending_context["guided_teaching"] == winning_context["guided_teaching"]
    assert learning.sessions.get(sid)["active_turn_id"] is None
    with db_session() as db:
        assert db.scalar(
            select(func.count()).select_from(Message).where(Message.session_id == sid)
        ) == len(prior_message_ids) + len(winning_messages)


def worked_example_output():
    value = output(source_ids=[EXAMPLE_RESOURCE])
    value["analysis"]["source_ids"] = [EXAMPLE_RESOURCE]
    value["analysis"]["techniques"] = [
        {
            "name": "扣除固定费用后处理数量关系",
            "applicability": "总价由固定费用与单价乘数量构成时使用。",
            "origin": "worked_example",
            "source_ids": [EXAMPLE_RESOURCE],
        }
    ]
    return value


@pytest.mark.asyncio
async def test_search_summary_does_not_authorize_claiming_a_worked_example_was_read(
    teaching,
):
    learning, runner, sid = teaching
    runner.search_query = "配送费"
    runner.responses = [worked_example_output()]
    with pytest.raises(ValueError, match="搜索摘要不等于读过解析"):
        await start(learning, sid)
    assert EXAMPLE_RESOURCE in {item["resource_id"] for item in runner.search_results}
    assert learning.sessions.get(sid).get("guidance") is None


@pytest.mark.asyncio
async def test_actual_example_read_receipt_is_persisted_and_reused_next_turn(teaching):
    learning, runner, sid = teaching
    runner.read_resource = EXAMPLE_RESOURCE
    runner.responses = [worked_example_output()]
    await start(learning, sid)
    assert EXAMPLE_RESOURCE in raw_workspace(sid)["read_resource_ids"]
    runner.read_resource = None
    runner.responses = [worked_example_output()]
    result, _, _ = await submit(
        learning, sid, "reply", "请继续讲刚才参考例题中的固定费用思路。"
    )
    assert {item["id"] for item in result["guidance"]["sources"]} == {EXAMPLE_RESOURCE}
    assert raw_workspace(sid)["analysis"]["techniques"][0]["origin"] == "worked_example"


@pytest.mark.asyncio
@pytest.mark.parametrize("new_problem_help", [0, 1])
async def test_full_original_explanation_can_attach_transfer_without_leaking_its_private_solution(
    teaching, new_problem_help
):
    learning, runner, sid = teaching
    await start(learning, sid)
    private_transfer = "PRIVATE_TRANSFER_REFERENCE:5n+10=45;n=7"
    runner.responses = [
        output(
            action="worked_example",
            analysis=None,
            content="原题把本数设为x，得到3x+6=42，减去6再除以3，得到12本。接下来换一个新情境。",
            question="请独立写出新题的数量关系。",
            help_level=3,
            transfer_problem={
                "stem": "附带新题：门票每张5元，手续费10元，总价45元，购买几张？",
                "reference_solution": private_transfer,
                "target": "独立检查固定费用建模",
                **({"help_level": new_problem_help} if new_problem_help else {}),
            },
        )
    ]
    result, _, _ = await submit(learning, sid, "explain", "请讲清原题后给一道新题。")
    state = raw_workspace(sid)
    assert state["assisted"]
    # 同轮自由示范不能认证与新题提示完全隔离，按辅助条件保守记录。
    assert state["transfer"]["assisted"]
    assert state["transfer"]["reference_solution"] == private_transfer
    assert state["transfer"]["help_level"] >= max(1, new_problem_help)
    assert result["guidance"]["transfer"]["stem"] in result["message"]["content"]
    assert private_transfer not in json.dumps(result, ensure_ascii=False)
    assert_public(result["guidance"])
    runner.responses = [
        output(
            action="complete",
            analysis=None,
            question="",
            understanding="demonstrated",
            student_input_kind="attempt",
        )
    ]
    await submit(learning, sid, "reply", "5n+10=45，所以n=7。")
    assert not raw_workspace(sid)["evidence"][-1]["independent_response"]


@pytest.mark.asyncio
async def test_real_concept_details_read_closes_catalog_and_material_citation_chain(
    teaching,
):
    learning, runner, sid = teaching
    runner.read_concept = QUANTITY_CONCEPT
    result, _, _ = await start(learning, sid)
    sources = {item["id"]: item for item in result["guidance"]["sources"]}
    assert set(sources) == {QUANTITY_CONCEPT, "junior_algebra:fixture"}
    assert sources[QUANTITY_CONCEPT]["origin"] == "course_catalog"
    assert sources[QUANTITY_CONCEPT]["course_id"] == "junior_algebra"
    assert (
        sources["junior_algebra:fixture"]["source_ref"]
        == "fixture://self_authored/junior_algebra"
    )
    assert {item["id"] for item in result["message"]["citations"]} == set(sources)
    assert raw_workspace(sid)["analysis"]["source_ids"] == list(sources)
    assert "mechanics:fixture" not in sources


@pytest.mark.asyncio
@pytest.mark.parametrize("expectation", ["optional", "none"])
async def test_explanation_can_pause_without_question_and_resume_same_guidance(
    teaching, expectation
):
    learning, runner, sid = teaching
    runner.responses = [
        output(
            action="explain",
            content="运费固定为6元，与购买本数无关；书本费用才随本数改变。",
            question="",
            response_expectation=expectation,
            help_level=1,
        )
    ]
    first, _, _ = await start(learning, sid)
    guidance = first["guidance"]
    assert guidance["response_expectation"] == expectation
    assert guidance["pending_question"] == ""
    assert guidance["pending_question_id"] is None
    assert guidance["status"] == "ready"
    assert learning.guidance.active(learning.sessions.get(sid))
    restored = LearningService(TeachingRunner()).sessions.get(sid)
    assert restored["guidance"] == guidance

    runner.responses = [
        output(
            action="explain",
            analysis=None,
            content="从总价中减去固定运费，剩下的正是所有书本的费用。",
            question="",
            response_expectation="none",
            student_input_kind="question",
        )
    ]

    async def emit(*_):
        pass

    result = await learning.turn(
        sid,
        MessageRequest(content="为什么要从总价中减去运费？", request_id=str(uuid4()),
            wait_id=learning.wait(sid)["wait_id"]),
        str(uuid4()),
        emit,
    )
    assert result["guidance"]["id"] == guidance["id"]
    assert result["guidance"]["turn_count"] == 2
    assert runner.calls[-1]["skill"] == "guided_explanation"
    assert (
        runner.calls[-1]["payload"]["student_message"] == "为什么要从总价中减去运费？"
    )
    assert raw_workspace(sid)["evidence"][-1]["student_input_kind"] == "question"
    assert not raw_workspace(sid)["evidence"][-1]["independent_response"]


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["", " \n\t"])
async def test_required_answer_without_concrete_question_cannot_commit(
    teaching, question
):
    learning, runner, sid = teaching
    runner.responses = [output(response_expectation="answer", question=question)]
    with pytest.raises(ValueError):
        await start(learning, sid)
    assert learning.sessions.get(sid).get("guidance") is None
    assert learning.sessions.get(sid)["active_turn_id"] is None


@pytest.mark.asyncio
async def test_complete_output_clears_response_expectation(teaching):
    learning, runner, sid = teaching
    await start(learning, sid)
    runner.responses = [output(action="complete", analysis=None, question="")]
    result, _, _ = await submit(learning, sid, "reply", "谢谢，我先复习这一步。")
    assert result["guidance"]["status"] == "completed"
    assert result["guidance"]["response_expectation"] == "none"
    assert result["guidance"]["pending_question_id"] is None


async def independent_transfer(learning, runner, sid):
    """通过真实服务发布未提供帮助的新题，供证据边界回归使用。"""
    await start(learning, sid)
    runner.responses = [
        output(
            action="transfer",
            analysis=None,
            question="请写出新题的数量关系，并求购买袋数。",
            transfer_problem={
                "stem": "每袋苹果6元，另付9元配送费，共39元，买了几袋？",
                "reference_solution": "6n+9=39，n=5。",
                "target": "独立建立固定费用数量关系",
            },
        )
    ]
    await submit(learning, sid, "transfer")
    return raw_workspace(sid)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["reply", "hint"])
async def test_continuing_current_problem_cannot_replace_transfer_or_reset_help(
    teaching, action
):
    learning, runner, sid = teaching
    await independent_transfer(learning, runner, sid)
    runner.responses = [output(action="hint", analysis=None, help_level=2)]
    await submit(learning, sid, "hint", "请解释当前题的数量关系。")
    before = raw_workspace(sid)
    assert before["transfer"]["assisted"]
    runner.responses = [
        output(
            action="transfer",
            analysis=None,
            transfer_problem={
                "stem": "每本书8元，另付12元运费，总共68元，共买几本？",
                "reference_solution": "8n+12=68，n=7。",
                "target": "数量关系",
            },
        )
    ]
    with pytest.raises(ValueError, match="继续当前题"):
        await submit(learning, sid, action, "请只重述同一道验证题，不要换题。")
    assert raw_workspace(sid) == before


@pytest.mark.asyncio
async def test_feedback_help_does_not_rewrite_already_submitted_independent_attempt(
    teaching,
):
    learning, runner, sid = teaching
    before = await independent_transfer(learning, runner, sid)
    assert before["pending_question_id"]
    runner.responses = [
        output(
            action="explain",
            analysis=None,
            content="你已独立列出正确关系。两边同时减去9，再除以6，即得n=5。",
            question="",
            response_expectation="none",
            help_level=3,
            student_input_kind="attempt",
            understanding="demonstrated",
        )
    ]
    await submit(learning, sid, "reply", "我先列6n+9=39。")
    state = raw_workspace(sid)
    evidence = deepcopy(state["evidence"][-1])
    context = evidence["response_context"]
    assert evidence["is_attempt"]
    assert evidence["transfer_response"] and evidence["independent_response"]
    assert not evidence["assisted_before_response"]
    assert context["workspace_id"] == before["id"]
    assert context["workspace_version"] == before["version"]
    assert context["problem_id"] == before["transfer"]["id"]
    assert context["problem_kind"] == "transfer"
    assert context["question_id"] == before["pending_question_id"]
    assert context["question"] == before["pending_question"]
    assert context["assisted"] is False
    assert context["help_level"] == 0
    assert context["support_event_ids"] == []
    assert state["transfer"]["assisted"]
    assert state["support_history"]

    runner.responses = [
        output(
            action="complete",
            analysis=None,
            question="",
            student_input_kind="attempt",
            understanding="demonstrated",
        )
    ]
    await submit(learning, sid, "reply", "我按刚才的方法得到n=5。")
    later = raw_workspace(sid)
    assert later["evidence"][-2] == evidence
    assert later["evidence"][-1]["assisted_before_response"]
    assert not later["evidence"][-1]["independent_response"]
    assert later["evidence"][-1]["response_context"]["support_event_ids"]
    with db_session() as db:
        assert db.scalar(select(func.count()).select_from(StudentState)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,kind",
    [
        ("为什么要先减去9？", "question"),
        ("懂了", "acknowledgement"),
        ("我晚点再回来", "other"),
    ],
)
async def test_followup_or_acknowledgement_is_not_an_independent_attempt(
    teaching, text, kind
):
    learning, runner, sid = teaching
    await independent_transfer(learning, runner, sid)
    runner.responses = [
        output(
            action="explain",
            analysis=None,
            content="这次先保留你的反馈，尚没有收到可供检查的解题过程。",
            question="",
            response_expectation="none",
            student_input_kind=kind,
            understanding="demonstrated",
        )
    ]
    await submit(learning, sid, "reply", text)
    state = raw_workspace(sid)
    evidence = state["evidence"][-1]
    assert evidence["raw_input"] == text
    assert evidence["student_input_kind"] == kind
    assert evidence["model_understanding"] == "demonstrated"
    assert not evidence["is_attempt"]
    assert not evidence["independent_response"]
    assert state["transfer"]["response_count"] == 0
    with db_session() as db:
        assert db.scalar(select(func.count()).select_from(StudentState)) == 0


@pytest.mark.asyncio
async def test_independent_attempt_can_be_wrong_without_becoming_mastery(teaching):
    learning, runner, sid = teaching
    await independent_transfer(learning, runner, sid)
    runner.responses = [
        output(
            action="hint",
            analysis=None,
            help_level=1,
            student_input_kind="attempt",
            understanding="needs_help",
            observation="学生独立尝试，但把固定配送费也乘上了袋数。",
        )
    ]
    result, _, _ = await submit(learning, sid, "reply", "我觉得应该列(6+9)n=39。")
    evidence = raw_workspace(sid)["evidence"][-1]
    assert evidence["is_attempt"] and evidence["independent_response"]
    assert evidence["model_understanding"] == "needs_help"
    assert evidence["assessment_method"] == "model_observation_not_program_proof"
    assert "mastery" not in result["guidance"]
    with db_session() as db:
        assert db.scalar(select(func.count()).select_from(StudentState)) == 0


@pytest.mark.asyncio
async def test_failed_attempt_retry_preserves_submission_context_without_public_leak(
    teaching,
):
    learning, runner, sid = teaching
    before = await independent_transfer(learning, runner, sid)
    request = GuidanceRequest(
        action="reply", text="我独立列出6n+9=39。", request_id=str(uuid4())
    )
    events = []

    async def emit(name, data):
        events.append((name, data))

    runner.fail = True
    with pytest.raises(RuntimeError, match="模拟模型"):
        await run_guidance(learning,sid, request, str(uuid4()), emit)
    assert raw_workspace(sid) == before
    with db_session() as db:
        messages = [
            item
            for item in db.scalars(select(Message).where(Message.session_id == sid))
            if item.metadata_json.get("_native_request", {}).get("request_id") == request.request_id
        ]
        assert len(messages) == 1 and messages[0].content == request.text
        frozen = deepcopy(messages[0].metadata_json["_guidance_response_context"])
    assert frozen["workspace_version"] == before["version"]
    assert frozen["problem_id"] == before["transfer"]["id"]
    assert frozen["question_id"] == before["pending_question_id"]
    assert not frozen["assisted"]
    assert learning.sessions.get(sid)["active_turn_id"] is None

    runner.fail = False
    runner.responses = [
        output(
            action="explain",
            analysis=None,
            content="等式两边减去9，再同时除以6，就能得到购买了5袋。",
            question="",
            response_expectation="none",
            help_level=3,
            student_input_kind="attempt",
        )
    ]
    result = await run_guidance(learning,sid, request, str(uuid4()), emit)
    evidence = raw_workspace(sid)["evidence"][-1]
    assert runner.calls[-1]["payload"]["submitted_response_context"] == frozen
    assert evidence["response_context"] == frozen
    assert evidence["independent_response"]
    with db_session() as db:
        messages = [
            item
            for item in db.scalars(select(Message).where(Message.session_id == sid))
            if item.metadata_json.get("_native_request", {}).get("request_id") == request.request_id
        ]
        assert len(messages) == 1
        assert messages[0].metadata_json["_guidance_response_context"] == frozen
    public = json.dumps(
        [events, result, learning.sessions.get(sid)], ensure_ascii=False
    )
    assert "_guidance_response_context" not in public
    assert "support_event_ids" not in public
    assert_public(result)


@pytest.mark.asyncio
async def test_failed_old_attempt_cannot_be_reinterpreted_after_another_turn(teaching):
    learning, runner, sid = teaching
    await independent_transfer(learning, runner, sid)
    request = GuidanceRequest(
        action="reply", text="先列6n+9=39。", request_id=str(uuid4())
    )

    async def emit(*_):
        pass

    runner.fail = True
    with pytest.raises(RuntimeError, match="模拟模型"):
        await run_guidance(learning,sid, request, str(uuid4()), emit)
    runner.fail = False
    runner.responses = [
        output(
            action="explain",
            analysis=None,
            content="9元是固定费用，因此不会随购买袋数改变。",
            question="",
            response_expectation="none",
            student_input_kind="question",
        )
    ]
    await submit(learning, sid, "reply", "为什么9元不乘上袋数？")
    before_retry = raw_workspace(sid)
    calls = len(runner.calls)
    with pytest.raises(ValueError, match="会话已变化"):
        await run_guidance(learning,sid, request, str(uuid4()), emit)
    assert len(runner.calls) == calls
    assert raw_workspace(sid) == before_retry
    assert learning.sessions.get(sid)["active_turn_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("new_problem_help", [None, 0])
async def test_transfer_top_level_help_belongs_to_new_problem(
    teaching, new_problem_help
):
    learning, runner, sid = teaching
    await start(learning, sid)
    problem = {
        "stem": "每袋苹果6元，另付9元配送费，共39元，买了几袋？",
        "reference_solution": "6n+9=39，n=5。",
        "target": "根据已给方法建立固定费用数量关系",
        "publication_mode": "assisted",
    }
    if new_problem_help is not None:
        problem["help_level"] = new_problem_help
    runner.responses = [
        output(
            action="transfer",
            analysis=None,
            content="新题可以先从总费用扣掉固定配送费，再用剩下的费用除以单价。",
            question="请根据这个方法列出新题方程。",
            help_level=2,
            transfer_problem=problem,
        )
    ]
    _, transfer_request, _ = await submit(learning, sid, "transfer")
    state = raw_workspace(sid)
    transfer_id = state["transfer"]["id"]
    assert state["transfer"]["assisted"]
    support = [
        item
        for item in state["support_history"]
        if item["request_id"] == transfer_request.request_id
    ]
    assert support and all(event["problem_id"] == transfer_id for event in support)
    assert max(event["help_level"] for event in support) >= 2
    assert any("扣掉固定配送费" in event["content"] for event in support)

    runner.responses = [
        output(
            action="complete",
            analysis=None,
            question="",
            student_input_kind="attempt",
            understanding="demonstrated",
        )
    ]
    await submit(learning, sid, "reply", "根据提示可列6n+9=39，所以n=5。")
    evidence = raw_workspace(sid)["evidence"][-1]
    assert evidence["is_attempt"] and evidence["assisted_before_response"]
    assert not evidence["independent_response"]
    assert evidence["response_context"]["problem_id"] == transfer_id
    assert evidence["response_context"]["help_level"] >= 2
    assert set(evidence["response_context"]["support_event_ids"]) == {
        event["id"] for event in support
    }


@pytest.mark.asyncio
async def test_unassisted_new_transfer_keeps_prior_problem_support_separate(teaching):
    learning, runner, sid = teaching
    await independent_transfer(learning, runner, sid)
    runner.responses = [output(action="hint", analysis=None, help_level=1)]
    await submit(learning, sid, "hint", "请提示这道苹果题。")
    previous = raw_workspace(sid)
    old_transfer_id = previous["transfer"]["id"]
    old_support = deepcopy(previous["support_history"])
    assert previous["transfer"]["assisted"]
    runner.responses = [
        output(
            action="transfer",
            analysis=None,
            content="接下来换一道新题，独立写出数量关系。",
            question="请写出新题方程并求购票张数。",
            help_level=0,
            transfer_problem={
                "stem": "每张门票8元，另付12元服务费，总共68元，买了几张？",
                "reference_solution": "8n+12=68，n=7。",
                "target": "撤去帮助后独立建立数量关系",
            },
        )
    ]
    await submit(learning, sid, "transfer")
    state = raw_workspace(sid)
    assert state["transfer"]["id"] != old_transfer_id
    assert not state["transfer"]["assisted"]
    assert state["support_history"] == old_support
    runner.responses = [
        output(
            action="complete",
            analysis=None,
            question="",
            student_input_kind="attempt",
            understanding="demonstrated",
        )
    ]
    await submit(learning, sid, "reply", "8n+12=68，所以n=7。")
    evidence = raw_workspace(sid)["evidence"][-1]
    assert evidence["independent_response"]
    assert not evidence["assisted_before_response"]
    assert evidence["response_context"]["support_event_ids"] == []


@pytest.mark.asyncio
async def test_publication_suppresses_wrong_model_draft_and_publishes_evidence_note(
    teaching, monkeypatch
):
    learning, runner, sid = teaching
    await independent_transfer(learning, runner, sid)
    runner.responses = [
        output(
            action="hint",
            analysis=None,
            content="配送费9元只支付一次，可以先区分这笔费用和购买费用。",
            question="",
            response_expectation="none",
            help_level=0,
        )
    ]
    await submit(learning, sid, "hint")
    assert raw_workspace(sid)["transfer"]["assisted"]
    raw_draft = "未经校验的错误草稿：你这次是在没有任何提示的情况下独立完成的。"
    original_run = runner.run

    async def emit_wrong_draft(*args, **kwargs):
        await kwargs["emit"](
            "text_delta", {"stream_id": "raw-draft", "delta": raw_draft}
        )
        await kwargs["emit"]("text_reset", {"stream_id": "raw-draft"})
        await kwargs["emit"]("agent_started", {"run_id": "fixture-progress"})
        return await original_run(*args, **kwargs)

    monkeypatch.setattr(runner, "run", emit_wrong_draft)
    runner.responses = [
        output(
            action="complete",
            analysis=None,
            content="6n+9=39的数量关系成立，求得n=5，代回等式也成立。",
            question="",
            student_input_kind="attempt",
            observation="列式、求解及代回检验均完整。",
            understanding="demonstrated",
        )
    ]
    result, _, events = await submit(
        learning, sid, "reply", "我减去9还剩30，30除以6得到5袋。"
    )
    public_text = json.dumps([result, events], ensure_ascii=False)
    assert raw_draft not in public_text
    deltas = [value for name, value in events if name == "text_delta"]
    assert deltas
    assert all(value["delivery_mode"] == "validated_publication" for value in deltas)
    assert "".join(value["delta"] for value in deltas) == result["message"]["content"]
    assert not any(name == "text_reset" for name, _ in events)
    assert any(name == "agent_started" for name, _ in events)
    evidence = raw_workspace(sid)["evidence"][-1]
    note = result["message"]["metadata"]["evidence_feedback"]
    assert result["message"]["metadata"]["delivery_mode"] == "validated_publication"
    assert note == evidence["evidence_feedback"]
    assert note["assisted_before_response"] == evidence["assisted_before_response"]
    assert note["independent_response"] == evidence["independent_response"]
    assert note["condition"] == "assisted"
    assert note["statement"] in result["message"]["content"]
    assert "辅助条件" in note["statement"]
    with db_session() as db:
        row = db.get(Message, result["message"]["id"])
        assert (
            row.metadata_json["_guidance_publication_audit"]["suppressed_model_deltas"]
            == 1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_field", ["content", "observation"])
async def test_wrong_model_independence_claim_cannot_commit_or_stream(
    teaching, monkeypatch, bad_field
):
    learning, runner, sid = teaching
    await independent_transfer(learning, runner, sid)
    before = raw_workspace(sid)
    with db_session() as db:
        assistant_count = db.scalar(
            select(func.count())
            .select_from(Message)
            .where(Message.session_id == sid, Message.role == "assistant")
        )
    wrong_claim = "你这次是在没有任何提示的情况下独立完成的。"
    original_run = runner.run

    async def emit_rejected_draft(*args, **kwargs):
        await kwargs["emit"](
            "text_delta", {"stream_id": "rejected-draft", "delta": wrong_claim}
        )
        return await original_run(*args, **kwargs)

    monkeypatch.setattr(runner, "run", emit_rejected_draft)
    rejected_output = output(
        action="complete",
        analysis=None,
        content="数量关系成立，接下来可以核对计算。",
        question="",
        student_input_kind="attempt",
        observation="计算过程完整。",
    )
    rejected_output[bad_field] = wrong_claim
    runner.responses = [rejected_output]
    events = []

    async def emit(name, value):
        events.append((name, value))

    request = GuidanceRequest(
        action="reply", text="我先减去9再除以6，得到5袋。", request_id=str(uuid4())
    )
    with pytest.raises(ValueError, match="提交前帮助快照"):
        await run_guidance(learning,sid, request, str(uuid4()), emit)
    assert raw_workspace(sid) == before
    assert not any(name == "text_delta" for name, _ in events)
    assert wrong_claim not in json.dumps(events, ensure_ascii=False)
    assert learning.sessions.get(sid)["active_turn_id"] is None
    with db_session() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.session_id == sid, Message.role == "assistant")
            )
            == assistant_count
        )
        # 原始学生输入仍保留，拒绝教师输出不会吞掉真实作答。
        rows = list(db.scalars(select(Message).where(Message.session_id == sid)))
        assert any(
            row.metadata_json.get("_native_request", {}).get("request_id") == request.request_id
            for row in rows
        )


@pytest.mark.asyncio
async def test_independent_transfer_discards_unlabelled_structure_before_publication(
    teaching,
):
    learning, runner, sid = teaching
    await start(learning, sid)
    leaked_structure = (
        "固定费用+随人数变化的费用=总支出。先扣掉固定部分，再除以每人费用。"
    )
    problem_stem = "科技馆每人票价15元，另付一次性讲解费40元，共花费190元，去了多少人？"
    runner.responses = [
        output(
            action="transfer",
            analysis=None,
            content=leaked_structure,
            question="请按刚才给出的结构列出方程。",
            help_level=0,
            transfer_problem={
                "stem": problem_stem,
                "reference_solution": "15x+40=190，x=10。",
                "target": "建立数量关系并求人数",
                "publication_mode": "independent",
                "help_level": 0,
            },
        )
    ]
    result, _, events = await submit(learning, sid, "transfer")
    assert result["message"]["content"] == (
        "下面是一道新题。\n\n" + problem_stem + "\n\n请独立作答，写出你的思路和过程。"
    )
    public = json.dumps(
        [result, events, learning.sessions.get(sid)], ensure_ascii=False
    )
    assert leaked_structure not in public
    assert "请按刚才给出的结构" not in public
    assert "suppressed_independent_fields" not in public
    assert "_guidance_publication_audit" not in public
    deltas = [value for name, value in events if name == "text_delta"]
    assert "".join(value["delta"] for value in deltas) == result["message"]["content"]
    state = raw_workspace(sid)
    transfer_id = state["transfer"]["id"]
    assert not state["transfer"]["assisted"]
    assert state["transfer"]["help_level"] == 0
    assert not any(
        item["problem_id"] == transfer_id for item in state["support_history"]
    )
    with db_session() as db:
        row = db.get(Message, result["message"]["id"])
        discarded = row.metadata_json["_guidance_publication_audit"][
            "suppressed_independent_fields"
        ]
        assert discarded["content"] == leaked_structure
        assert discarded["help_level"] == 0
    runner.responses = [
        output(
            action="complete",
            analysis=None,
            content="先扣除40元后得到150元，再除以15元得到10人，代回总费用成立。",
            question="",
            student_input_kind="attempt",
            observation="算术过程和实际人数均合理。",
            understanding="demonstrated",
        )
    ]
    answered, _, _ = await submit(
        learning, sid, "reply", "190里面先拿走讲解的40，还剩150。每人15，所以是10个人。"
    )
    evidence = raw_workspace(sid)["evidence"][-1]
    assert evidence["response_context"]["problem_id"] == transfer_id
    assert evidence["response_context"]["support_event_ids"] == []
    assert evidence["response_context"]["help_level"] == 0
    assert not evidence["assisted_before_response"]
    assert evidence["independent_response"]
    note = answered["message"]["metadata"]["evidence_feedback"]
    assert note == evidence["evidence_feedback"]
    assert note["condition"] == "independent_recorded_conditions"
    assert note["statement"] in answered["message"]["content"]


@pytest.mark.asyncio
async def test_independent_transfer_suppresses_all_public_workspace_teaching_channels(
    teaching,
):
    learning, runner, sid = teaching
    await start(learning, sid)
    leaked_focus = "先扣45再除12"
    leaked_summary = "新题建模结构：图书钱加快递费等于总花费，12x+45=525。"
    leaked_title = "新题方法标题：先减去快递费再除以书价"
    leaked_target = "新题目标提示：把525扣除45后除以12"
    with db_session() as db:
        material = db.get(Material, "junior_algebra:fixture")
        material.title = leaked_title
    analysis = deepcopy(output()["analysis"])
    analysis["summary"] = leaked_summary
    runner.read_resource = OWN_RESOURCE
    runner.responses = [
        output(
            action="transfer",
            content="这是一道新题。",
            question="请尝试。",
            focus=leaked_focus,
            analysis=analysis,
            transfer_problem={
                "stem": "每本书12元，另付快递费45元，共525元，购买了多少本书？",
                "reference_solution": "12x+45=525，x=40。",
                "target": leaked_target,
                "publication_mode": "independent",
            },
        )
    ]
    result, _, events = await submit(learning, sid, "transfer")
    public_text = json.dumps(
        [result, events, learning.sessions.get(sid)], ensure_ascii=False
    )
    for leaked in (leaked_focus, leaked_summary, leaked_title, leaked_target):
        assert leaked not in public_text
    assert result["guidance"]["focus"] == ""
    assert result["guidance"]["analysis_summary"] == ""
    assert result["guidance"]["sources"] == []
    assert result["message"]["citations"] == []
    deltas = [value["delta"] for name, value in events if name == "text_delta"]
    assert "".join(deltas) == result["message"]["content"]
    state = raw_workspace(sid)
    assert state["analysis"]["summary"] == leaked_summary
    assert state["analysis"]["reference_solution"] == PRIVATE_REFERENCE
    assert any(source["title"] == leaked_title for source in state["sources"])
    assert state["transfer"]["target"] == leaked_target
    transfer_id = state["transfer"]["id"]
    assert not state["transfer"]["assisted"]
    assert not any(
        event["problem_id"] == transfer_id for event in state["support_history"]
    )
    with db_session() as db:
        row = db.get(Message, result["message"]["id"])
        discarded = row.metadata_json["_guidance_publication_audit"][
            "suppressed_independent_fields"
        ]
        assert discarded["focus"] == leaked_focus
        assert discarded["source_ids"]
    runner.read_resource = None
    runner.responses = [
        output(
            action="explain",
            content="减去快递费后得到480元，除以每本12元得到40本，计算成立。",
            question="",
            response_expectation="none",
            analysis=None,
            student_input_kind="attempt",
            observation="数量关系与计算过程成立。",
            understanding="demonstrated",
        )
    ]
    _, _, _ = await submit(
        learning, sid, "reply", "书钱是525减45，等于480元，再除以12，买了40本。"
    )
    evidence = raw_workspace(sid)["evidence"][-1]
    assert not evidence["assisted_before_response"]
    assert evidence["independent_response"]
    assert evidence["response_context"]["support_event_ids"] == []


@pytest.mark.asyncio
async def test_independent_transfer_never_streams_tool_arguments_or_free_text(
    teaching, monkeypatch
):
    learning, runner, sid = teaching
    await start(learning, sid)
    leaked_query = "先扣45再除12"
    leaked_extra = "方法是固定费用加随数量变化的费用等于总量"
    original_run = runner.run

    async def emit_tool_details(*args, **kwargs):
        await kwargs["emit"](
            "tool_completed",
            {
                "agent": "exercise",
                "name": "search_teaching_resources",
                "status": "completed",
                "duration_ms": 12.5,
                "item_count": 1,
                "arguments": {"query": leaked_query},
                "extra_free_text": leaked_extra,
            },
        )
        return await original_run(*args, **kwargs)

    monkeypatch.setattr(runner, "run", emit_tool_details)
    runner.responses = [
        output(
            action="transfer",
            analysis=None,
            content="下面是一道新题。",
            question="请尝试。",
            transfer_problem={
                "stem": "每本书12元，另付快递费45元，共525元，购买了多少本书？",
                "reference_solution": "12x+45=525，x=40。",
                "target": "解答新题",
                "publication_mode": "independent",
            },
        )
    ]
    result, _, events = await submit(learning, sid, "transfer")
    serialized = json.dumps([result, events], ensure_ascii=False)
    assert leaked_query not in serialized
    assert leaked_extra not in serialized
    progress = [value for name, value in events if name == "tool_completed"]
    assert len(progress) == 1
    assert progress[0]["name"] == "search_teaching_resources"
    assert progress[0]["status"] == "completed"
    assert progress[0]["duration_ms"] == 12.5
    assert "arguments" not in progress[0]
    assert "extra_free_text" not in progress[0]
    state = raw_workspace(sid)
    assert not state["transfer"]["assisted"]
    assert state["transfer"]["help_level"] == 0
    assert not any(
        event["problem_id"] == state["transfer"]["id"]
        for event in state["support_history"]
    )


@pytest.mark.asyncio
async def test_normalized_historical_transfer_keeps_conservative_source_label_and_old_evidence(
    teaching,
):
    from app.domains.guidance.evidence import capture_response_context

    learning, runner, sid = teaching
    await independent_transfer(learning, runner, sid)
    legacy = raw_workspace(sid)
    # 一次性迁移已完成；在线路径只消费统一格式及保守来源标签。
    legacy["skill_version"] = "3"
    legacy["transfer"]["assisted"] = True
    legacy["transfer"]["help_level"] = 1
    legacy["transfer"]["assistance_basis"] = "legacy_unverified"
    legacy["assisted"] = True
    old_evidence = deepcopy(legacy["evidence"])
    transfer_id = legacy["transfer"]["id"]
    with db_session() as db:
        row = db.get(LearningSession, sid)
        row.pending_context = {**row.pending_context, "guided_teaching": legacy}
    runner.responses = [
        output(
            action="explain",
            analysis=None,
            content="减去配送费9元后剩30元，每袋6元，所以是5袋，计算成立。",
            question="",
            response_expectation="none",
            student_input_kind="attempt",
            observation="数量关系与计算成立。",
            understanding="demonstrated",
        )
    ]
    result, _, _ = await submit(
        learning, sid, "reply", "先从39元去掉9元配送费，再用30除以6，得到5袋。"
    )
    state = raw_workspace(sid)
    assert state["evidence"][: len(old_evidence)] == old_evidence
    evidence = state["evidence"][-1]
    context = evidence["response_context"]
    assert context["problem_id"] == transfer_id
    assert context["assisted"]
    assert context["assistance_basis"] == "legacy_unverified"
    assert context["policy_version"] == "3"
    assert evidence["assisted_before_response"]
    assert not evidence["independent_response"]
    migration_events = [
        event
        for event in state["support_history"]
        if event.get("detection_method") == "server_legacy_policy"
        and event["problem_id"] == transfer_id
    ]
    assert not migration_events
    note = result["message"]["metadata"]["evidence_feedback"]
    assert note["assisted_before_response"]
    assert not note["independent_response"]
    assert "历史" in note["statement"]
    assert "无法确认" in note["statement"] or "未核验" in note["statement"]
    assert note["statement"] in result["message"]["content"]
    runner.responses = [
        output(
            action="transfer",
            analysis=None,
            content="下面是一道新题。",
            question="请尝试。",
            transfer_problem={
                "stem": "每盒彩笔8元，另付一次性配送费12元，共108元，买了多少盒彩笔？",
                "reference_solution": "8x+12=108，x=12。",
                "target": "解答新题",
                "publication_mode": "independent",
            },
        )
    ]
    _, _, _ = await submit(learning, sid, "transfer")
    fresh = raw_workspace(sid)
    assert fresh["transfer"]["id"] != transfer_id
    assert not fresh["transfer"]["assisted"]
    assert fresh["transfer"]["help_level"] == 0
    fresh_context = capture_response_context(fresh)
    assert not fresh_context["assisted"]
    assert fresh_context["support_event_ids"] == []
    assert fresh["evidence"][: len(old_evidence)] == old_evidence


@pytest.mark.asyncio
async def test_workspace_persists_references_and_typed_support_not_body_copies(teaching):
    learning,runner,sid=teaching
    await start(learning,sid)
    await submit(learning,sid,"hint")
    await submit(learning,sid,"reply","我需要先减去运费。")
    with db_session() as db:
        raw=db.get(LearningSession,sid).pending_context["guided_teaching"]
        assert not {"dialogue","support_history","prior_observations","evidence"}.intersection(raw)
        assert "analysis" not in raw
        assert len(raw["history_refs"])==3
        rows=[db.get(Message,identifier) for identifier in raw["history_refs"]]
        assert all("raw_input" not in m.metadata_json["_guidance_observation"] for m in rows)
        assert all("raw_input_source" in m.metadata_json["_guidance_observation"] for m in rows)
        assert any(m.metadata_json["_guidance_support"] for m in rows)
        latest=db.get(Message,rows[-1].metadata_json["_guidance_observation"]["input_message_id"])
        assert latest.content=="我需要先减去运费。" and "_guidance_input_text" not in latest.metadata_json
        assert rows[-1].metadata_json["_guidance_observation"]["raw_input_source"] == "_native_request.event.text"
    restored=raw_workspace(sid)
    assert restored["evidence"][-1]["raw_input"]=="我需要先减去运费。"
    assert restored["support_history"] and all(e["problem_id"] for e in restored["support_history"])
    with db_session() as db:
        from app.domains.memory.models import MemoryFact
        assert all("_guidance_analysis" not in json.dumps(f.payload,ensure_ascii=False) for f in db.scalars(select(MemoryFact)))
