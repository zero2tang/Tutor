"""把当前任务和可摘要历史分开，固定字段从不交给摘要器重写。"""

from copy import deepcopy
from hashlib import sha256
import json


def fingerprint(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def history_unit(kind, index, data):
    return {
        "source_ref": f"{kind}:{index}:{fingerprint(data)[:16]}",
        "kind": kind,
        "data": deepcopy(data),
    }


def deduplicate_verbatim(value, *, anchors=None, root="data"):
    """同一投影中的长原话只保留一份；引用含精确路径和指纹，不伪装成新证据。"""
    seen = dict(anchors or {})

    def visit(item, location):
        if isinstance(item, str) and len(item) >= 256:
            if item in seen:
                return {"verbatim_ref": seen[item], "sha256": fingerprint(item)}
            seen[item] = location
            return item
        if isinstance(item, dict):
            return {key: visit(child, location + "." + key) for key, child in item.items()}
        if isinstance(item, list):
            return [visit(child, f"{location}[{index}]") for index, child in enumerate(item)]
        return item

    return visit(value, root)


class ContextBuilder:
    def __init__(self, keep_recent_turns=4):
        self.keep = keep_recent_turns

    def project(self, payload, role, *, minimal=False):
        """列表只按完整条目分离；真实原话、题干、帮助快照和目标不按字符截断。"""
        fixed = deepcopy(payload)
        history = []
        memory = fixed.get("learning_memory")
        if isinstance(memory, dict):
            if memory.get("schema_version") == "business-memory-v2":
                # 所有作用域正文只走统一预算入口；旧调用方的副本不能绕过预算。
                for field in ("student_observations", "memories"):
                    if isinstance(fixed.get(field), list):
                        fixed[field] = [item for item in fixed[field] if not isinstance(item, dict)
                            or (not item.get("scope_key") and item.get("kind") not in {
                                "global_memory", "course_memory", "concept_memory", "student_memory", "session_experience"})]
            # 兼容业务旧字段，但同一版两类记忆只经统一入口注入一次。
            canonical = [memory.get("student_memory"), *memory.get("session_experiences", [])]
            signatures = {
                (item.get("content"), item.get("version"), item.get("session_id"))
                for item in canonical if isinstance(item, dict)
            }
            observations = fixed.get("student_observations")
            if isinstance(observations, list):
                fixed["student_observations"] = [
                    item for item in observations
                    if not isinstance(item, dict) or item.get("kind") not in {"student_memory", "session_experience"}
                    or (item.get("content"), item.get("version"), item.get("session_id")) not in signatures
                ]
            if minimal:
                # 可选的课程和旁支概念记忆可按来源进入摘要；当前作答仍在固定任务中。
                if memory.get("schema_version") == "business-memory-v2":
                    course = memory.get("course_memory")
                    if course:
                        unit = history_unit("memory.course", 0, course)
                        history.append(unit)
                        memory["course_memory_ref"] = unit["source_ref"]
                        memory["course_memory"] = None
                    concepts = memory.get("concept_memories", [])
                    if len(concepts) > 1:
                        moved = [history_unit("memory.concept", i + 1, item) for i, item in enumerate(concepts[1:])]
                        history.extend(moved)
                        memory["concept_memory_refs"] = [item["source_ref"] for item in moved]
                        memory["concept_memories"] = concepts[:1]
                experiences = memory.get("session_experiences", [])
                history.extend(history_unit("memory.session_experience", i, item) for i, item in enumerate(experiences))
                memory["session_experience_refs"] = [
                    {key: item[key] for key in ("session_id", "version", "covered_seq") if key in item}
                    for item in experiences
                ]
                memory["session_experiences"] = []
        recent = fixed.get("recent_messages")
        if isinstance(recent, list):
            # 普通对话两条消息构成一轮；最新用户原话仍由独立字段完整保留。
            cutoff = max(0, len(recent) - self.keep * 2)
            history.extend(history_unit("recent_messages", i, item) for i, item in enumerate(recent[:cutoff]))
            fixed["recent_messages"] = recent[cutoff:]
        workspace = fixed.get("teaching_workspace")
        if isinstance(workspace, dict):
            counts = {}
            # 旧内部分析是可重建的模型推导；当前原题和本次作答才是固定评估材料。
            if workspace.get("analysis") is not None:
                unit = history_unit("guidance.analysis", workspace.get("version", 0), workspace.pop("analysis"))
                history.append(unit)
                workspace["analysis_history_ref"] = unit["source_ref"]
            for field in ("dialogue", "evidence", "support_history", "prior_observations"):
                items = workspace.get(field)
                if not isinstance(items, list):
                    continue
                cutoff = len(items) if minimal else max(0, len(items) - self.keep)
                history.extend(history_unit(f"guidance.{field}", i, item) for i, item in enumerate(items[:cutoff]))
                workspace[field] = items[cutoff:]
                counts[field] = {"total": len(items), "recent_full": len(items) - cutoff}
            # dialogue.student_message与evidence.raw_input经常完全相同，不重复携带长答案。
            anchors = {
                fixed[key]: key for key in ("student_message", "user_message", "student_answer", "answer_text")
                if isinstance(fixed.get(key), str) and len(fixed[key]) >= 256
            }
            # 联合去重，保持所有引用均落在本固定投影或始终保留的当前原话上。
            recent_records = {field: workspace[field] for field in ("dialogue", "evidence", "support_history", "prior_observations") if field in workspace}
            deduplicated = deduplicate_verbatim(recent_records, anchors=anchors, root="teaching_workspace")
            workspace.update(deduplicated)
            workspace["context_projection"] = {
                "counts": counts,
                "mode": "minimal_fixed_task" if minimal else "recent_full_interactions",
                "notice": "旧记录进入可重建历史区；当前题、待答问题和提交前帮助快照保持完整。verbatim_ref指向本投影同一份原话。",
            }
        # 不把 assessment 的原始答案或 guidance 的 request.text 混入可摘要区。
        return fixed, history

    @staticmethod
    def state_fingerprint(payload):
        keys = (
            "session_goals", "goal_state", "learning_progress", "exercise_context",
            "submitted_response_context", "pending_question", "question", "question_id",
        )
        value = {key: payload[key] for key in keys if key in payload}
        workspace = payload.get("teaching_workspace")
        if isinstance(workspace, dict):
            value["guidance"] = {
                key: workspace.get(key) for key in (
                    "id", "version", "problem", "transfer", "pending_question_id",
                    "pending_question", "help_level", "assisted",
                )
            }
        memory = payload.get("learning_memory")
        if isinstance(memory, dict):
            value["memory_versions"] = memory
        return fingerprint(value)
