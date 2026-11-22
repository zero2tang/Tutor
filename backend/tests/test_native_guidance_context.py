"""指导活动返回主 Agent 时，已发布交互进入唯一父消息 State。"""

import asyncio

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.application.learning.service import LearningService
from app.domains.learning.schemas import MessageRequest
from test_guided_teaching import teaching, output, PRIVATE_REFERENCE
from test_native_session_graph import ScriptedRunner, emit


def test_guidance_public_interactions_survive_return_to_supervisor(teaching, tmp_path):
    _, guidance_runner, sid = teaching
    supervisor = ScriptedRunner()
    observed = []

    class CombinedRunner:
        tools = guidance_runner.tools

        async def run(self, role, *args, **kwargs):
            if role == "supervisor":
                observed.append(list(kwargs.get("messages") or []))
                return await supervisor.run(role, *args, **kwargs)
            return await guidance_runner.run(role, *args, **kwargs)

    raw = "  我先把6当成每本运费。\n所以不知道怎样列式。  "
    guidance_runner.responses = [output(), output(content="6元是整次配送的固定费用，与本数无关。",
        question="现在再表示书本的费用。", student_input_kind="attempt")]

    async def scenario():
        path = str(tmp_path / "activity-context.sqlite")
        runner = CombinedRunner()
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            learning = LearningService(runner, checkpointer=saver)
            await learning.turn(sid, MessageRequest(content="先确认本次学习目标。", request_id="before-guidance-context"), "root-before", emit)
            started = await learning.execute(sid, {"kind": "guidance_action", "action": "start",
                "problem": {"stem": "每本3元，运费6元，共42元，买了几本？"}, "request_id": "context-start-guidance"}, "guide-start", emit)
            response = await learning.turn(sid, MessageRequest(content=raw, request_id="context-raw-guidance",
                wait_id=started["session"]["pending_interaction"]["wait_id"]), "guide-reply", emit)
            await learning.execute(sid, {"kind": "guidance_action", "action": "close", "request_id": "context-close-guidance",
                "wait_id": response["session"]["pending_interaction"]["wait_id"], "expected_version": response["guidance"]["version"]}, "guide-close", emit)
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            restored = LearningService(runner, checkpointer=saver)
            await restored.turn(sid, MessageRequest(content="刚才我把哪一项费用理解错了？", request_id="after-guidance-context"), "root-after", emit)
            messages = observed[-1]
            assert any(message.content == raw for message in messages)
            assert any("6元是整次配送的固定费用" in message.content for message in messages)
            assert PRIVATE_REFERENCE not in str(messages)
            identifiers = [message.id for message in messages]
            assert len(identifiers) == len(set(identifiers))

    asyncio.run(scenario())
