"""在调用方事务中维护目标图、版本和补救返回点，绝不写学生能力状态。"""

from copy import deepcopy
from hashlib import sha256
import json
from uuid import uuid4

from sqlalchemy import update

from app.domains.assessment.models import Attempt
from app.domains.sessions.models import Message
from app.domains.sessions.models import SessionGoalState
from app.domains.sessions.schemas import GoalNode, SessionGoalUpdate


class GoalVersionConflict(ValueError):
    pass


def public_goals(state: SessionGoalState) -> dict:
    return {
        "session_id": state.session_id,
        "root_goal_id": state.root_goal_id,
        "active_goal_id": state.active_goal_id,
        "nodes": deepcopy(state.nodes),
        "return_stack": deepcopy(state.return_stack),
        "next_action": state.next_action,
        "version": state.version,
    }


def ensure_session_goals(db, session) -> SessionGoalState:
    """旧会话首次访问时从本会话目标建根，不复制其他会话的执行状态。"""
    state = db.get(SessionGoalState, session.id)
    if state is not None:
        return state
    root_id = "goal:" + str(uuid4())
    root = GoalNode(
        goal_id=root_id,
        description=session.goal,
        source_ref="session:" + session.id,
        success_criteria=["完成学生在本会话明确提出的学习任务；活动完成不等于知识掌握。"],
        status="active",
    ).model_dump()
    state = SessionGoalState(
        session_id=session.id,
        root_goal_id=root_id,
        active_goal_id=root_id,
        nodes=[root],
        return_stack=[],
        next_action="确认具体学习步骤与成功标准。",
        version=0,
    )
    db.add(state)
    db.flush()
    return state


def context_projection(state: SessionGoalState | dict) -> dict:
    """模型只读当前祖先路径、依赖及暂停索引；全图仍可从会话快照恢复。"""
    data = public_goals(state) if isinstance(state, SessionGoalState) else deepcopy(state)
    nodes = {node["goal_id"]: node for node in data["nodes"]}
    needed = {data["root_goal_id"]}
    cursor = data.get("active_goal_id")
    while cursor and cursor not in needed:
        needed.add(cursor)
        cursor = nodes[cursor]["parent_id"]
    needed.update(
        dependency
        for identifier in list(needed)
        for dependency in nodes[identifier]["prerequisites"]
    )
    return {
        **{key: data[key] for key in ("session_id", "root_goal_id", "active_goal_id", "version", "next_action", "return_stack")},
        "nodes": [node for node in data["nodes"] if node["goal_id"] in needed],
        "other_goal_index": [
            {**{key: node[key] for key in ("goal_id", "parent_id", "status")}, "description": node["description"][:160]}
            for node in data["nodes"] if node["goal_id"] not in needed
        ],
    }


def get_session_goal(db, session, goal_id: str) -> dict:
    """为目标短索引提供按ID回读；归属仅由服务端传入的当前会话决定。"""
    state = ensure_session_goals(db, session)
    for node in state.nodes:
        if node["goal_id"] == goal_id:
            return {"session_id": session.id, "version": state.version, "node": deepcopy(node)}
    raise ValueError("目标不属于当前会话")


def _validate_reference(db, session, reference: str) -> None:
    kind, _, identifier = reference.partition(":")
    if kind == "session" and identifier == session.id:
        return
    model = {"message": Message, "attempt": Attempt}.get(kind)
    if model:
        source = db.get(model, identifier)
        if source is not None and source.session_id == session.id:
            return
    raise ValueError("目标来源或证据必须引用当前会话中存在的session/message/attempt")


def _pending_identity(session) -> dict:
    """只保存身份与指纹，不把包含答案的内部题目契约公开到目标状态。"""
    context = session.pending_context or {}
    contract = context.get("contract") or {}
    return {
        "pending_question_id": session.pending_question_id,
        "discussion_exercise_id": context.get("discussion_exercise_id"),
        "contract_fingerprint": sha256(json.dumps(contract, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
    }


def _check_graph(nodes, root_id, active_id, *, has_pending=False):
    if len(nodes) > 32:
        raise ValueError("会话目标超过32个，请结束或拆分本次任务")
    for node in nodes.values():
        parent = node["parent_id"]
        if parent and parent not in nodes:
            raise ValueError("目标父节点不属于当前会话")
        if not parent and node["goal_id"] != root_id and node["status"] != "cancelled":
            raise ValueError("当前会话只允许一个非取消的整体目标")
        if any(identifier not in nodes for identifier in node["prerequisites"]):
            raise ValueError("依赖目标不属于当前会话")
    for identifier in nodes:
        seen, cursor = set(), identifier
        while cursor:
            if cursor in seen:
                raise ValueError("目标父子关系存在环")
            seen.add(cursor)
            if len(seen) > 5:
                raise ValueError("目标树最多四层子目标，请拆分任务")
            cursor = nodes[cursor]["parent_id"]
    execution = {key: list(node["prerequisites"]) for key, node in nodes.items()}
    for identifier, node in nodes.items():
        if node["parent_id"]:
            execution[node["parent_id"]].append(identifier)
    for identifier, node in nodes.items():
        if node["status"] in {"completed", "cancelled"}:
            continue
        cursor = identifier
        while nodes[cursor]["parent_id"]:
            cursor = nodes[cursor]["parent_id"]
            if nodes[cursor]["status"] in {"completed", "cancelled"}:
                raise ValueError("执行中的节点不能挂在已结束的父目标下")
        if cursor != root_id:
            raise ValueError("执行中的节点必须属于本会话当前整体目标")
    running = {key for key, node in nodes.items() if node["status"] in {"active", "awaiting_student"}}
    if active_id:
        if active_id not in nodes or nodes[active_id]["status"] not in {"active", "awaiting_student", "blocked"}:
            raise ValueError("当前目标必须存在并处于可执行或等待状态")
        if running - {active_id}:
            raise ValueError("本期仅允许一条当前执行路径")
        if any(
            nodes[dependency]["status"] != "completed"
            for dependency in nodes[active_id]["prerequisites"]
        ):
            raise ValueError("当前目标的必要依赖尚未完成")
    elif running:
        raise ValueError("存在执行中的节点时必须指定当前目标")
    for identifier, node in nodes.items():
        if node["status"] == "completed":
            if any(nodes[dependency]["status"] not in {"completed", "cancelled"} for dependency in execution[identifier]):
                raise ValueError("子目标或依赖未完成，不能宣告父目标完成")
    if nodes[root_id]["status"] == "completed" and has_pending:
        raise ValueError("存在待答题时不能结束整体目标")


def build_goal_candidate(
    db, session, state, change: dict, *, continuation=None
) -> dict:
    """纯候选构建便于在模型输出修复环节校验，成功后才能提交。"""
    proposal = SessionGoalUpdate.model_validate(change).model_dump()
    if proposal["expected_version"] != state.version:
        raise GoalVersionConflict("会话目标版本已变化，旧候选已丢弃；最新session_goals=" + json.dumps(context_projection(state), ensure_ascii=False))
    data = public_goals(state)
    nodes = {node["goal_id"]: node for node in data["nodes"]}
    if len({node["goal_id"] for node in proposal["nodes"]}) != len(proposal["nodes"]):
        raise ValueError("同一目标更新不能重复提交goal_id")
    for node in proposal["nodes"]:
        old = nodes.get(node["goal_id"])
        if old and node["prerequisites"] != old["prerequisites"]:
            raise ValueError("既有前置条件不能改写")
        if old is None and any(
            identifier not in nodes or nodes[identifier]["status"] != "completed"
            for identifier in node["prerequisites"]
        ):
            raise ValueError("新目标只允许引用已完成的前置条件，不再建立跨分支依赖图")
        if old and old["status"] in {"completed", "cancelled"} and node["status"] != old["status"]:
            raise ValueError("已完成或已取消节点不能重新激活，请新建子目标")
        if (
            old
            and old["status"] in {"completed", "cancelled"}
            and any(
                old[key] != node[key]
                for key in (
                    "parent_id",
                    "description",
                    "source_ref",
                    "success_criteria",
                    "prerequisites",
                )
            )
        ):
            raise ValueError("已结束节点的成功标准和历史不能被改写")
        if node["goal_id"] == data["root_goal_id"] and node["description"] != session.goal:
            raise ValueError("结构化节点不能更换整体目标，必须使用有当前用户原话的goal_update")
        _validate_reference(db, session, node["source_ref"])
        for reference in node["evidence_refs"]:
            _validate_reference(db, session, reference)
        nodes[node["goal_id"]] = node
    active_id = proposal["active_goal_id"]
    operation = proposal["operation"]
    stack = data["return_stack"]
    if operation == "push_remediation":
        target = proposal["remediation_goal_id"]
        origin = data["active_goal_id"]
        if not origin or not target or target not in nodes or target == origin:
            raise ValueError("补救必须提供不同于当前目标的有效remediation_goal_id")
        if len(stack) >= 4:
            raise ValueError("补救返回栈过深，请先回到已有步骤")
        progress = (session.pending_context or {}).get("learning_progress", {})
        stack.append({
            "goal_id": origin,
            "remediation_goal_id": target,
            **_pending_identity(session),
            "current_step": progress.get("current_step", ""),
            "next_step": progress.get("next_step", ""),
        })
        nodes[origin]["status"] = "blocked"
        active_id = target
        nodes[target]["status"] = "active"
    elif operation == "resume":
        if not stack:
            raise ValueError("没有可以恢复的原任务")
        point = stack[-1]
        if nodes[point["remediation_goal_id"]]["status"] not in {"completed", "cancelled"}:
            raise ValueError("补救未结束，不能弹出返回点")
        identity = _pending_identity(session)
        if any(point[key] != identity[key] for key in identity):
            raise ValueError("原题或待答契约已变化，不能恢复过时返回点")
        active_id = point["goal_id"]
        nodes[active_id]["status"] = "active"
        stack.pop()
    else:
        # 省略当前节点表示保持；完成当前节点后允许显式选择下一节点。
        active_id = active_id or data["active_goal_id"]
        if active_id and nodes.get(active_id, {}).get("status") in {"completed", "cancelled"}:
            active_id = None
        if stack and active_id != data["active_goal_id"]:
            raise ValueError("补救期间切换回原任务必须使用resume，不能绕过返回校验")
    if active_id and active_id not in nodes:
        raise ValueError("当前目标不属于本会话")
    if active_id and active_id != data["active_goal_id"] and operation == "update":
        previous = data["active_goal_id"]
        if previous and nodes[previous]["status"] in {"active", "awaiting_student"}:
            nodes[previous]["status"] = "planned"
    if active_id and nodes[active_id]["status"] == "planned":
        nodes[active_id]["status"] = "active"
    if continuation and active_id:
        status = continuation.get("status")
        if status == "awaiting_student":
            nodes[active_id]["status"] = "awaiting_student"
        elif status == "awaiting_clarification":
            nodes[active_id]["status"] = "blocked"
    _check_graph(nodes, data["root_goal_id"], active_id, has_pending=bool(session.pending_question_id))
    return {**data, "nodes": list(nodes.values()), "active_goal_id": active_id, "return_stack": stack,
            "next_action": proposal["next_action"] or data["next_action"]}


def _commit_candidate(db, state, candidate):
    expected = state.version
    values = {key: candidate[key] for key in ("root_goal_id", "active_goal_id", "nodes", "return_stack", "next_action")}
    changed = any(getattr(state, key) != value for key, value in values.items())
    if not changed:
        return state
    result = db.execute(update(SessionGoalState).where(
        SessionGoalState.session_id == state.session_id,
        SessionGoalState.version == expected,
    ).values(**values, version=expected + 1).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise GoalVersionConflict("会话目标被并发修改，旧候选已丢弃")
    db.expire(state)
    db.refresh(state)
    return state


def apply_goal_update(db, session, change=None, *, continuation=None, overall_changed=False):
    """与规划、会话版本同事务提交；目标更新不会替换原题或更新掌握度。"""
    state = ensure_session_goals(db, session)
    if overall_changed:
        previous_version = state.version
        if change and change["expected_version"] != previous_version:
            raise GoalVersionConflict("整体目标变化前的版本不匹配")
        data = public_goals(state)
        for node in data["nodes"]:
            if node["status"] not in {"completed", "cancelled"}:
                node["status"] = "cancelled"
        root_id = "goal:" + str(uuid4())
        root = GoalNode(goal_id=root_id, description=session.goal, source_ref="session:" + session.id,
                        success_criteria=["完成学生明确更换后的本会话任务。"], status="active").model_dump()
        data.update(root_goal_id=root_id, active_goal_id=root_id, nodes=[*data["nodes"], root], return_stack=[],
                    next_action=(continuation or {}).get("next_step", "确认新目标的具体步骤。"))
        _commit_candidate(db, state, data)
        # 变更整体目标后模型尚不知道新根ID，下一轮再分解；拒绝同时覆盖旧树。
        if change:
            raise ValueError("更换整体目标时session_goal_update应为空，读取新根后再分解")
        return state
    if change is not None:
        returning = deepcopy(state.return_stack[-1]) if change.get("operation") == "resume" and state.return_stack else None
        candidate = build_goal_candidate(db, session, state, change, continuation=continuation)
        if returning:
            # 模型的补救讲解可能已改写continuation，恢复点必须从压栈事实重建。
            pending = deepcopy(session.pending_context or {})
            progress = dict(pending.get("learning_progress") or {})
            for target, source in (("current_step", "current_step"), ("next_step", "next_step")):
                if returning.get(source):
                    progress[target] = returning[source]
            pending["learning_progress"] = progress
            session.pending_context = pending
            if not change.get("next_action") and returning.get("next_step"):
                candidate["next_action"] = returning["next_step"]
        return _commit_candidate(db, state, candidate)
    if continuation:
        # 兼容旧Plan：只投影流程停点，不把completed_steps伪造成掌握证据。
        data = public_goals(state)
        for node in data["nodes"]:
            if node["goal_id"] == data["active_goal_id"]:
                if continuation["status"] == "awaiting_student":
                    node["status"] = "awaiting_student"
                elif continuation["status"] == "awaiting_clarification":
                    node["status"] = "blocked"
        data["next_action"] = continuation["next_step"]
        return _commit_candidate(db, state, data)
    return state


def sync_workflow(db, session, *, workflow: str, workspace_id: str, description: str,
                  current_step: str, next_action: str, status="awaiting_student",
                  success_criteria=None, remediation: dict | None = None):
    """供绕过规划器的教学工作区调用；参数必须来自已提交业务状态。

    remediation为{key, description, success_criteria}；补问消失后自动结束该活动并回原题，
    这仅表示补问活动结束，不能解释为相应知识已经掌握。
    """
    state = ensure_session_goals(db, session)
    data = public_goals(state)
    # 这是导航标签，完整外部题干仍由工作区和续学上下文固定字段保存。
    if len(description) > 1000:
        description = description[:960] + "…（完整题干保存在当前教学工作区）"
    identifier = "workflow:" + sha256(f"{workflow}:{workspace_id}".encode()).hexdigest()[:32]
    nodes = {node["goal_id"]: node for node in data["nodes"]}
    node = nodes.get(identifier) or GoalNode(
        goal_id=identifier, parent_id=data["root_goal_id"], description=description,
        source_ref="session:" + session.id,
        success_criteria=success_criteria or ["完成本题教学活动；掌握情况仅由独立评估证据决定。"],
    ).model_dump()
    node["status"] = status
    node["description"] = description
    for value in nodes.values():
        if value["goal_id"] != identifier and value["status"] in {"active", "awaiting_student"}:
            value["status"] = "planned"
    nodes[identifier] = node
    stack = data["return_stack"]
    active_id = identifier if status not in {"completed", "cancelled"} else None
    top = stack[-1] if stack else None
    if remediation:
        same_return = bool(top and top.get("workflow_id") == identifier)
        if same_return and top.get("remediation_key") != remediation["key"]:
            raise ValueError("尚有未结束的补问，不能覆盖其返回点")
        remedial_id = top["remediation_goal_id"] if same_return else "remediation:" + str(uuid4())
        if not same_return:
            stack.append({"goal_id": identifier, "workflow_id": identifier,
                          "remediation_goal_id": remedial_id, "remediation_key": remediation["key"], "workspace_id": workspace_id,
                          "current_step": current_step, **_pending_identity(session)})
        nodes[identifier]["status"] = "blocked"
        nodes[remedial_id] = GoalNode(
            goal_id=remedial_id, parent_id=identifier, description=remediation["description"],
            source_ref="session:" + session.id,
            success_criteria=remediation.get("success_criteria") or ["完成补问后回到原题原步骤，不据此升级掌握度。"],
            status="awaiting_student",
        ).model_dump()
        active_id = remedial_id
    elif top and top.get("workflow_id") == identifier:
        if top["workspace_id"] != workspace_id or any(top[key] != value for key, value in _pending_identity(session).items()):
            raise ValueError("补救原题身份变化，不能恢复旧返回点")
        nodes[top["remediation_goal_id"]]["status"] = "completed"
        stack.pop()
    _check_graph(nodes, data["root_goal_id"], active_id, has_pending=bool(session.pending_question_id))
    data.update(nodes=list(nodes.values()), active_goal_id=active_id, return_stack=stack, next_action=next_action)
    return _commit_candidate(db, state, data)
