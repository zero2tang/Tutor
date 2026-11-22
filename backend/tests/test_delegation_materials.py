"""资料引用池退役后，子任务使用自己的受控检索，边界继续由服务验证。"""
import pytest
from pydantic import ValidationError
from app.application.agent.tools import KnowledgeArguments,ExerciseArguments,TeachingTools
from app.agent.tools.contracts import ToolContext


def test_transfer_selector_is_rejected():
    with pytest.raises(ValidationError):
        KnowledgeArguments(task="讲解",concept_ids=["math:c1"],selected_material_ids=["mat_"+"a"*32])


def test_exercise_transfer_selector_is_rejected():
    with pytest.raises(ValidationError):
        ExerciseArguments(operation="generate",concept_ids=["math:c1"],selected_material_ids=[])


def test_context_has_no_inherited_material_authority():
    with pytest.raises(TypeError):ToolContext(material_transfer_receipt={"accepted_ids":["forged"]})


def test_child_can_only_use_base_registry_read_tools():
    tools=TeachingTools(None)
    assert "selected_material_ids" not in str(KnowledgeArguments.model_json_schema())
    assert "call_knowledge_agent" not in {item.name for item in tools.for_role("exercise",handler=object())}
