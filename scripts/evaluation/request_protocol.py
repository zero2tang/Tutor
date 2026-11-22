"""验收脚本的 v2 请求构造：从公开会话读取停点，重放保留原始绑定。"""

from uuid import uuid4


async def request_body(client, method, path, body, bindings):
    if method.upper() != "POST":
        return body
    route = path.split("?", 1)[0].rstrip("/")
    parts = route.split("/")
    if len(parts) < 3 or parts[-3] != "sessions":
        return body
    action = parts[-1]
    if action not in {"messages", "practice", "answers", "batch-answers", "skip", "guidance"}:
        return body
    result = dict(body or {})
    result.setdefault("request_id", str(uuid4()))
    key = (parts[-2], result["request_id"])
    # 首次提交绑定服务端当前停点；重复提交不能改绑为后来生成的另一道题。
    if key not in bindings:
        session_path = "/".join(parts[:-1])
        response = await client.get(session_path)
        response.raise_for_status()
        bindings[key] = response.json().get("pending_interaction")
    pending = bindings[key]
    if pending and action != "practice":
        result.setdefault("wait_id", pending["wait_id"])
        if action == "answers":
            if not pending.get("question_id"):
                raise AssertionError("服务端公开停点未提供单题身份")
            result.setdefault("question_id", pending["question_id"])
    elif action in {"answers", "batch-answers", "skip"}:
        raise AssertionError("服务端没有可提交的等待停点，脚本不能猜测题目身份")
    return result
