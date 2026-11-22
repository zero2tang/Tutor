"""校验工具组闭合及摘要来源，程序校验不声称能证明语义忠实。"""


def closed_message_groups(messages):
    """返回完整消息组的切点；工具调用及全部回执保持一个单元。"""
    groups = []
    index = 0
    while index < len(messages):
        start = index
        message = messages[index]
        calls = message.get("tool_calls") or []
        if calls:
            expected = {call["id"] for call in calls}
            if len(expected) != len(calls):
                raise ValueError("工具调用 ID 重复，不能压缩该消息前缀")
            index += 1
            received = set()
            while index < len(messages) and messages[index].get("role") == "tool":
                call_id = messages[index].get("tool_call_id")
                if call_id not in expected or call_id in received:
                    raise ValueError("工具回执不属于当前调用组或重复")
                received.add(call_id)
                index += 1
            if received != expected:
                # 当前批次尚未闭合，返回它之前的稳定前缀。
                break
        elif message.get("role") == "tool":
            raise ValueError("孤立工具回执不能作为安全压缩前缀")
        else:
            index += 1
        groups.append((start, index))
    return groups


def validate_source_refs(summary, units):
    expected = {unit["source_ref"] for unit in units}
    actual = set(summary.source_refs)
    if actual != expected:
        raise ValueError("摘要覆盖范围与输入来源不一致，拒绝候选")
