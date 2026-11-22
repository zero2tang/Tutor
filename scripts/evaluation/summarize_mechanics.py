"""从本次独立验收档案汇总角色运行、Tokens 和检索耗时。"""

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import select
from app.infrastructure.persistence.database import db_session
from app.infrastructure.persistence.execution_models import AgentRun
from app.domains.sessions.models import LearningSession


def main() -> None:
    reports = ROOT / "data/reports"
    report = json.loads(
        (reports / "mechanics_acceptance.json").read_text(encoding="utf-8")
    )
    assert report["complete_suite_run"], "只汇总完整测试运行"
    student = report["artifacts"]["student_id"]
    with db_session() as db:
        runs = db.scalars(
            select(AgentRun)
            .join(LearningSession, AgentRun.session_id == LearningSession.id)
            .where(LearningSession.student_id == student)
        ).all()
        metrics = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "student_id": student,
            "agent_runs": len(runs),
            "agent_status_counts": dict(Counter(run.status for run in runs)),
            "by_agent": {
                role: {
                    "runs": sum(run.agent == role for run in runs),
                    "prompt_tokens": sum(
                        run.prompt_tokens for run in runs if run.agent == role
                    ),
                    "completion_tokens": sum(
                        run.completion_tokens for run in runs if run.agent == role
                    ),
                    "duration_ms": round(
                        sum(run.duration_ms for run in runs if run.agent == role), 2
                    ),
                }
                for role in ["supervisor", "knowledge", "exercise", "assessment"]
            },
            "skills": dict(Counter(run.agent + ":" + run.skill for run in runs)),
            "tool_counts": dict(
                Counter(
                    tool["name"]
                    for run in runs
                    for tool in run.tool_calls
                    if tool["status"] == "completed"
                )
            ),
            "prompt_tokens": sum(run.prompt_tokens for run in runs),
            "completion_tokens": sum(run.completion_tokens for run in runs),
        }
    (reports / "mechanics_runtime_metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# 力学课程固定任务验收",
        "",
        f"时间：{report['generated_at']}。",
        "",
        f"结果：**{report['passed']}/{report['total']}（{report['success_rate']:.0%}）**，目标至少 {report['target_success_rate']:.0%}；串行总耗时 {report['duration_seconds']} 秒。",
        "",
        f"后端实现、技能和固定任务指纹：`{report['source_sha256']}`。",
        "",
        "| 任务 | 实际结果 | 耗时（秒） |",
        "|---|---|---:|",
    ]
    lines += [
        f"| {row['id']} {row['name']} | {row['status']} | {row['duration_seconds']} |"
        for row in report["tasks"]
    ]
    failed = [row for row in report["tasks"] if row["status"] == "failed"]
    if failed:
        lines += ["", "## 未通过项目与补充核查", ""]
        for row in failed:
            lines.append(f"- {row['id']}：`{row.get('error', '')}`。")
        supplemental_path = reports / "supplemental_idempotency.json"
        if supplemental_path.exists():
            supplemental = json.loads(supplemental_path.read_text(encoding="utf-8"))
            if supplemental.get("student_id") == student:
                diagnosis = supplemental["original_diagnosis"]
                lines += [
                    "",
                    f"T14 的原始判分为 {diagnosis['result']}，诊断置信度 {diagnosis['confidence']}，状态更新数 {diagnosis['state_update_count']}。评估器按单题证据不足给出低置信诊断，服务遵守 0.8 门槛，没有强行上调掌握度，因而未满足固定任务必须更新的断言。",
                    "",
                    "独立补充核查已验证：重复答案返回同一作答记录，计数与状态不变；同一提交标识更换答案被拒绝。补充核查不改变固定任务的成败统计。",
                    "",
                    "下一步应区分判分确定性、诊断证据强度和状态估计置信度；不能通过强行提高模型置信度来消除失败。详见 [补充幂等证据](supplemental_idempotency.json)。",
                ]
    lines += [
        "",
        "## 实际角色运行",
        "",
        "| 角色 | 运行次数 | 输入 Tokens | 输出 Tokens | 累积耗时（毫秒） |",
        "|---|---:|---:|---:|---:|",
    ]
    lines += [
        f"| {role} | {row['runs']} | {row['prompt_tokens']} | {row['completion_tokens']} | {row['duration_ms']} |"
        for role, row in metrics["by_agent"].items()
    ]
    lines += [
        "",
        f"角色运行状态：`{metrics['agent_status_counts']}`。成功工具调用：`{metrics['tool_counts']}`。",
        "",
        "Tokens 来自模型接口实际 usage；一次角色运行可能包含多轮工具调用。数字包括固定任务的模型消耗，不包括外部向量生成、网页人工验收和此前调试，不推算价格。测试为单机串行功能验收，不是并发压测，也不是教学正确率或学生学习增益评估。",
        "",
        "## 检索测量",
        "",
    ]
    for row in report["tasks"]:
        timings = row.get("evidence", {}).get("timings")
        if timings:
            lines.append(f"- {row['id']}：`{timings}`")
    lines += [
        "",
        "查询可能命中本地向量缓存；以上为本次实际时延，不能据此声称冷启动或生产延迟。",
        "",
        "机器证据：[完整任务结果](mechanics_acceptance.json)、[角色运行汇总](mechanics_runtime_metrics.json)、[进程重启恢复](restart_recovery.json)、[缺图边界](runtime_guards.json)、[补充幂等核查](supplemental_idempotency.json)。",
        "",
    ]
    (reports / "mechanics_acceptance.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
