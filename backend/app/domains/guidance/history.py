"""讲题历史按消息引用读取；正文只在消息中存放，帮助归属保留有类型回执。"""

from copy import deepcopy
from app.domains.sessions.models import Message

TRANSIENT = ("dialogue", "support_history", "evidence", "prior_observations")


def thin_workspace(state, message_id=None):
    value = {k: deepcopy(v) for k, v in state.items() if k not in {*TRANSIENT,"analysis"}}
    refs = list(value.get("history_refs", []))
    if message_id and message_id not in refs:
        refs.append(message_id)
    value["history_refs"] = refs
    return value


def span(text, value):
    if not value:
        return None
    start = text.find(value)
    return [start, start + len(value)] if start >= 0 else None


def support_receipt(event, content):
    value = {k: deepcopy(v) for k, v in event.items() if k not in {"content", "question"}}
    for field in ("content", "question"):
        value[field + "_span"] = span(content, event.get(field, ""))
        if event.get(field) and value[field + "_span"] is None:
            raise ValueError("帮助回执必须能定位到本次实际公开的正文片段")
    return value


def observation_receipt(evidence, user):
    value = {k: deepcopy(v) for k, v in evidence.items() if k != "raw_input"}
    value["input_message_id"] = user.id
    event = user.metadata_json["_native_request"]["event"]
    field = "content" if event["kind"] in {"message", "followup", "clarify", "change_goal"} else "text"
    value["raw_input_source"] = "_native_request.event." + field if field in event else None
    return value


def hydrate(db, state):
    value = deepcopy(state)
    value.update({key: [] for key in TRANSIENT})
    value["analysis"] = None

    def observation(message):
        entry = deepcopy((message.metadata_json or {}).get("_guidance_observation"))
        if not entry:
            return None
        user = db.get(Message, entry["input_message_id"])
        if not user or user.session_id != message.session_id:
            raise ValueError("讲题原答引用不属于当前会话")
        source = entry.pop("raw_input_source")
        if source == "content":
            entry["raw_input"] = user.content
        elif source is None:
            entry["raw_input"] = ""
        else:
            # 来源路径由服务端绑定，历史原话只读使用，不按旧格式重新猜测。
            raw = user.metadata_json
            for field in source.split("."):
                raw = raw[field]
            entry["raw_input"] = raw
        return entry

    for identifier in state["history_refs"]:
        message = db.get(Message, identifier)
        if not message or message.session_id != state.get("session_id", message.session_id):
            raise ValueError("讲题历史引用缺失或越界")
        if message.metadata_json.get("workspace_id") != state["id"]:
            raise ValueError("讲题消息不属于当前工作区")
        if message.metadata_json.get("_guidance_analysis") is not None:
            value["analysis"] = deepcopy(message.metadata_json["_guidance_analysis"])
        entry = observation(message)
        if entry:
            value["evidence"].append(entry)
        for receipt in message.metadata_json.get("_guidance_support", []):
            event = deepcopy(receipt)
            for field in ("content", "question"):
                bounds = event.pop(field + "_span")
                event[field] = (
                    message.content[bounds[0] : bounds[1]]
                    if bounds is not None
                    else ""
                )
            value["support_history"].append(event)
        if entry:
            public = message.metadata_json["_guidance_receipt"]["guidance"]
            bounds = message.metadata_json["_guidance_body_span"]
            value["dialogue"].append(
                {
                    "student_message": entry["raw_input"],
                    "requested_action": entry["requested_action"],
                    "content": message.content[bounds[0] : bounds[1]] if bounds is not None else "",
                    "question": public["pending_question"],
                    "question_id": public["pending_question_id"],
                    "response_expectation": public["response_expectation"],
                    "observation": entry["model_observation"],
                    "evidence_feedback": entry.get("evidence_feedback"),
                }
            )
    value["dialogue"] = value["dialogue"][-12:]
    for identifier in state.get("prior_message_ids", [])[-4:]:
        message = db.get(Message, identifier)
        if (
            message
            and message.session_id == state.get("session_id")
            and (entry := observation(message))
        ):
            value["prior_observations"].append(
                {
                    k: entry[k]
                    for k in (
                        "raw_input",
                        "model_observation",
                        "model_understanding",
                        "assessment_method",
                    )
                }
            )
    return value
