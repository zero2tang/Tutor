"""父模型决策通过后的公开答复组装，不负责委派执行和业务提交。"""


def present_delegations(outcomes, request_id, plan):
    """只在父 Agent 完成决策后统一展示正文、公开题目和接续安排。"""
    content, citations, result = [], [], {}
    metadata = {"request_id": request_id, "kind": "response"}
    for outcome in outcomes:
        item = outcome["result"]
        citations.extend(item.get("citations", []))
        if "lesson" in item:
            lesson = item["lesson"]
            content.append(
                f"## {lesson['title']}\n\n{lesson['content']}\n\n**想一想**：{lesson['follow_up']}"
            )
            result["lesson"], metadata["kind"] = lesson, "lesson"
        if "explanation" in item:
            content.append(item["explanation"]["content"])
            result["explanation"], metadata["kind"] = (
                item["explanation"],
                "explanation",
            )
        if "question" in item:
            result["question"] = metadata["question"] = item["question"]
            metadata.update(kind="question", question_id=item["question"]["id"])
            metadata["exercise_id"] = item.get("exercise_id")
        if "exercise_batch" in item:
            result["exercise_batch"] = metadata["exercise_batch"] = item[
                "exercise_batch"
            ]
            metadata["kind"] = "exercise_batch"
    content.append(plan["message"])
    if plan.get("continuation"):
        step = plan["continuation"]
        content.append(
            f"**当前进度**：{step['current_step']}\n\n**接下来**：{step['next_step']}"
        )
    metadata["delegations"] = [
        {
            "caller": o["role"],
            "tool": o["name"],
            "child_run_ids": o["result"]["child_run_ids"],
        }
        for o in outcomes
    ]
    if "question" in metadata:
        # 同时讲解与出题时保持可查询的出题记录身份，不能被外层lesson覆盖。
        metadata["kind"] = "exercise_delivery"
    if "exercise_batch" in metadata:
        metadata["kind"] = "exercise_batch"
    return (
        "\n\n".join(part for part in content if part),
        list({c["id"]: c for c in citations}.values())[:10],
        metadata,
        result,
    )
