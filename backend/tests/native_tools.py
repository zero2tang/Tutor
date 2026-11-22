"""领域工具测试入口：复用公开工具Schema，显式传递可信运行范围。"""

from langchain.tools import ToolRuntime
from app.agent.runtime.contracts import RuntimeContext


async def invoke_tool(tools, role, name, arguments, scope):
    selected = next((item for item in tools.for_role(role) if item.name == name), None)
    if selected is None:
        raise PermissionError("当前角色未获得该工具")
    if set(arguments) - set(selected.tool_call_schema.model_fields):
        raise ValueError("工具参数含未声明字段")
    arguments = selected.tool_call_schema.model_validate(arguments, strict=True).model_dump()
    context = RuntimeContext(scope=scope, role=role, run_id="test-read", root_run_id="test-root")
    runtime = ToolRuntime(state={}, context=context, config={}, stream_writer=lambda value: None,
        tool_call_id="test-call", store=None)
    result = await selected.coroutine(**arguments, runtime=runtime)
    if hasattr(tools, "observe"):
        result = await tools.observe(name, arguments, result, scope)
    return result
