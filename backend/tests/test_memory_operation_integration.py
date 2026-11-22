from app.domains.memory.models import MemoryJob
from app.domains.memory.scopes.models import SessionMemoryExtraction
from reference_helpers import put_scope
"""业务阶段决定何时更新记忆，模型决定提取内容；派生失败不影响业务结果。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.api.operation_memory import capture_operation_memory


@pytest.mark.parametrize("result,reason", [
    ({"plan": {"goal_update": "新目标"}}, "goal_changed"),
    ({"plan": {"session_goal_update": {"operation": "resume"}}}, "remediation_return"),
    ({"verdict": {"probe": True}}, "remediation_return"),
    ({"plan": {"session_goal_update": {"nodes": [{"status": "completed"}]}}}, "goal_completed"),
    ({"tutoring": {"status": "ready_for_transfer"}}, "teaching_stage_complete"),
    ({"guidance": {"closed": True}}, "guidance_stage_complete"),
    ({"tutoring": {"status": "ready_for_transfer"}, "replayed": True}, "turn"),
    ({}, "turn"),
])
def test_committed_phase_controls_priority_without_extracting_content(result, reason):
    memory = SimpleNamespace(capture=Mock())
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(services={"memory": memory})))
    capture_operation_memory(request, "owned-session", result)
    memory.capture.assert_called_once_with("owned-session", reason=reason, force=reason != "turn")


def test_memory_failure_does_not_change_committed_operation_result():
    memory = SimpleNamespace(capture=Mock(side_effect=RuntimeError("后台登记暂时失败")))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(services={"memory": memory})))
    result = {"tutoring": {"status": "ready_for_transfer"}, "message": {"content": "已保存"}}
    capture_operation_memory(request, "owned-session", result)
    assert result["message"]["content"] == "已保存"


def test_legacy_service_without_memory_keeps_working():
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(services={})))
    capture_operation_memory(request, "session", {})
