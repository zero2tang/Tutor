"""生成题的结构门槛、第三次语义复核和私有诊断记录。"""

from copy import deepcopy
import re

from app.infrastructure.persistence.execution_models import AgentRun
from app.domains.practice.schemas import SolutionReview
from app.domains.practice.answer_contracts import review_contract_issue
from app.domains.assessment.grading import normalize

ACCEPTED_VERDICTS = {"equivalent", "complementary"}


def answer_shape_issue(question_type, options, answer):
    """只检查可执行的答案格式，不比较两份答案是否数学等价。"""
    if not answer or any(not value.strip() for value in answer):
        return "答案不能为空"
    if question_type == "fill":
        return "单空填空必须返回一个统一答案" if len(answer) != 1 else ""
    if len(set(answer)) != len(answer) or not set(answer).issubset(options):
        return "选择题答案必须是互不重复、存在于选项中的大写标签"
    if question_type == "choice" and len(answer) != 1:
        return "单选题必须有且只有一个答案标签"
    if question_type == "multi_choice" and len(answer) < 2:
        return "多选题至少需要两个正确选项"
    return ""


def draft_issue(draft, question_type, cognitive_type, concept_ids):
    if draft["cognitive_type"] != cognitive_type:
        return "生成题的认知类型与子任务要求不一致"
    if re.search(r"如图|下图|图示|见图", draft["stem"]):
        return "当前工具不生成配图，题目必须能用文字条件独立求解"
    if draft["question_type"] != question_type:
        return "生成题型与请求不一致"
    if not draft["concept_ids"] or not set(draft["concept_ids"]).issubset(concept_ids):
        return "超出目标知识点范围"
    options = draft["options"]
    if question_type == "fill":
        if options:
            return "填空题选项必须为空"
    elif (
        set(options) != {"A", "B", "C", "D"}
        or len({normalize(value) for value in options.values()}) != 4
        or any(not value.strip() for value in options.values())
    ):
        return "选择题必须提供四个不为空且不重复的选项"
    return answer_shape_issue(question_type, options, draft["answer"])


class ExerciseReviewService:
    def __init__(self, runner):
        self.runner = runner

    async def reconcile(self, draft, independent, context, turn_id, emit, *, runtime_context=None, config=None):
        def validate(output):
            issue = review_contract_issue(draft, independent, output)
            if issue:
                raise ValueError(issue)
            if output["verdict"] in ACCEPTED_VERDICTS:
                issue = answer_shape_issue(
                    draft["question_type"], draft["options"], output["answer"]
                )
                if issue:
                    raise ValueError(issue)

        result = await self.runner.run(
            "exercise",
            "reconcile",
            {
                "task": "reconcile",
                "question": {
                    key: deepcopy(draft[key])
                    for key in (
                        "stem",
                        "options",
                        "question_type",
                        "concept_ids",
                        "cognitive_type",
                    )
                },
                "generation_solution": {
                    "answer": draft["answer"],
                    "explanation": draft["explanation"],
                },
                "independent_solution": deepcopy(independent),
                "instruction": "先分别原样回执两份输入answer，并明确评价该答案栏本身的correctness及与各自解析的consistency，再给总体理由和裁定。不能把解析中的正确结论当作已经修正的输入answer；任一答案栏错误或与解析矛盾时输出repair_required或conflict，answer=[]，不能以统一答案正确为由输出equivalent/complementary。只有两份答案本身正确且各自与解析一致时，数学等价输出equivalent，正确解法或各自明确的正确部分结论兼容且合并后完整回答原题时输出complementary。分数与小数、单位等价不按字符串差异拒绝；选择题原文可映射到同一选项，但不同完整标签集合不能取并集或悄悄选一方。缺条件、关键歧义或无合法解输出invalid；无法确认输出uncertain。所有非接受裁定answer=[]。不得改题干、选项或添加条件。吨、米等连续量允许分数或小数，未明确要求整数时不能因非整数拒绝；人数、本数等离散数量须检查整数和非负约束。",
            },
            SolutionReview,
            context,
            turn_id,
            emit=emit,
            use_tools=False,
            validate_output=validate,
            runtime_context=runtime_context,
            config=config,
        )
        validate(result["output"])
        result.pop("messages", None)
        return result


def record_generation_attempt(
    db,
    context,
    turn_id,
    assignment,
    attempt_number,
    draft_result,
    independent_result,
    review_result,
    issue,
    *,
    operation=None,
):
    """借用调用方事务保存私有证据，模型请求期间不持有此事务。"""
    review = review_result["output"] if review_result else None
    record = dict(
        session_id=context.session_id,
        course_id=context.course_id,
        turn_id=turn_id,
        task_index=(assignment or {}).get("index"),
        attempt_number=attempt_number,
        status=(
            "accepted"
            if review and review["verdict"] in ACCEPTED_VERDICTS
            else "rejected"
        ),
        reason=review["reason"] if review else issue,
        details={
            "generation": {
                "run_id": draft_result["run_id"],
                "output": deepcopy(draft_result["output"]),
            },
            "independent": (
                {
                    "run_id": independent_result["run_id"],
                    "output": deepcopy(independent_result["output"]),
                }
                if independent_result
                else None
            ),
            "review": (
                {"run_id": review_result["run_id"], "output": deepcopy(review)}
                if review_result
                else None
            ),
        },
        **(operation or {}),
    )
    run = db.get(AgentRun, draft_result["run_id"], with_for_update=True)
    if run is None:
        raise ValueError("生成运行记录不存在，拒绝丢失复核诊断")
    record["id"] = run.id
    run.private_diagnostics = {**(run.private_diagnostics or {}), "generation_attempt": record}
    db.flush()
    return run.id
