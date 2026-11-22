# Agent 自主安排练习与并行生成

## 行为

主 Agent 和知识点 Agent 都可以在结构化输出的 `exercise_batch` 中请求练习。
`tasks` 支持 1–6 项：一道题正常执行，多道题并行运行习题 Agent，最多同时生成三道。
出题不是每轮教学的必选动作，也不默认要求多题。前端题数和认知类型默认“由助手决定”；用户明确指定时，服务端校验生成计划遵守该要求。

每个任务包含：

| 字段 | 含义 |
| --- | --- |
| `concept_ids` | 本题考点，必须属于当前教学范围，课程和学生身份由服务端注入 |
| `cognitive_type` | `recall`、`understanding`、`application`，分别对应记忆、理解、应用 |
| `question_type` | `choice` 单选、`multi_choice` 多选、`fill` 单空填空 |
| `difficulty_range` | 可选的 0–100 内容难度目标区间，含 `minimum` 和 `maximum` |
| `instruction` | 本题考查情境、条件和能力要求 |

例：先安排一道理解题，或者分别安排记忆、理解、应用各一题。`tasks` 长度决定题数，无须另外声明一个可能不一致的数量。

难度区间是出题设计目标。生成题尚未经过现有内容量表独立标注时，公开 `difficulty_value` 仍为 null，页面明确显示“目标难度 / 实际难度待独立标注”。本次不提供数值区间达标保证，也不将目标难度解释为学生答错概率。认知类型、作答题型和内容难度分别建模。

## 执行链路

```text
主 Agent：Plan(action=practice, exercise_batch=...)
    或
知识点 Agent：Lesson(exercise_batch=...)
    ↓
服务端校验范围、数量与用户明确指定的偏好
    ↓
TaskGroup + Semaphore(3)
    ├─ 每题独立 AgentRunner.run：生成
    ├─ 每题独立 AgentRunner.run：不含参考答案的求解校验
    └─ 每题独立 AgentRunner.run：语义复核，整合等价或互补的正确解答
    ↓
全部成功、题号及去空白题干无重复 → 一次冻结会话练习组
    ↓
前端逐题填写 → 一次提交全部答案
    ↓
规则逐题判分 → 一次评估 Agent 运行 → 整组总结 + 逐题诊断
    ↓
同一事务保存逐题 Attempt、受约束证据、反馈消息和整组幂等结果
```

Agent 决定教学意图和任务参数，服务代码执行实际并发与写库；没有开放任意递归调用其他 Agent 的通用工具。每个子任务有独立消息、run_id 和 ToolContext 列表，仍共享原会话 turn_id。模型客户端原有三个并发请求的总限流继续有效。

生成题已增加第三次模型语义复核，统一答案和解析后入库，实质冲突最多重新生成一次，详见 [生成题的第三次模型语义复核](11_exercise_semantic_review.md)。

练习组路径不逐题调用评估角色进行作答前预测，也不在提交后额外调用主 Agent；提交时只有一次评估角色运行，负责全部题目及下一步建议。“一次运行”仍可能包含模型网络重试或契约修复，不承诺一次 HTTP 请求。

## 状态、恢复与边界

- 保留会话 `active_turn_id` 占用规则：外部不能并发修改同一会话，单次任务内部可以并行生成。
- 生成服务使用 `TaskGroup`。任何子任务失败，等待其余子任务取消后再释放会话，未成功的组不会成为待答练习。取消中的 AgentRun 记录为 `cancelled`。
- 成功生成并校验的单道题沿原有流程入库、建索引；组任务失败时，这些已验证题可能保留在题库，本次不清理它们。原子性保证针对会话待答组与作答证据。
- `pending_context.exercise_batch` 冻结每题完整内部契约、考点、认知类型、出题记录 ID 和辅助标记。旧 `pending_question_id` 仅以第一题作为非空兼容标记；批量接口从完整冻结组读取题目，浏览器的 `pending_question` 为 null。
- 每题另存带私有 `_exercise_contract` 的出题消息，主 Agent 可以通过已有历史题工具定位，当前上下文同时提供整组完整公开题目。公开消息仍过滤所有下划线私有字段。
- 前端使用按练习组 ID 隔离的 sessionStorage 保存学生草稿和幂等标识，刷新可恢复；标准答案不进入该草稿。未完成全部题目时禁用提交按钮，服务端再次校验覆盖范围、重复题号和空答案。
- 查看某题题解只将该题标记为辅助；待答期间的一般知识讲解保守标记整组为辅助。辅助、低置信度或未判分诊断不产生独立掌握证据。
- 整组提交后结束本组，未判分题保存回答和诊断但不更新掌握度。可以继续追问或重新安排练习，不保持一个部分完成的待答组。
- `BatchSubmission` 对 `(session_id, idempotency_key)` 建唯一约束，并保存规范化答案哈希。重试返回原结果，不重复评估或累计证据；同键不同答案或练习组会被拒绝。

## 接口和代码

`POST /api/sessions/{id}/messages` 保留原入口；可选 `exercise_count=1..6` 只代表用户明确偏好，缺省由 Agent 决定。既有单题接口继续可用。

`POST /api/sessions/{id}/batch-answers`：

```json
{
  "batch_id": "服务端返回的练习组 ID",
  "answers": [
    {"question_id": "math:generated:...", "answer": "A"}
  ],
  "idempotency_key": "同一份答案重试时保持不变的标识"
}
```

与其他学习操作一样，支持 SSE 或 `?stream=false`。新增生成进度事件 `batch_generation_started`、`batch_item_started`、`batch_item_completed`，以及公开组事件 `exercise_batch` 和提交完成事件 `batch_assessment_completed`。

核心模块：

- `domains/learning/schemas.py`：主/知识点请求和整组评估输出契约。
- `domains/practice/batches.py`：并行生成、范围校验和待答组冻结。
- `domains/assessment/batch.py`：一次整组评估与原子证据提交。
- `domains/assessment/rules.py`：单题与整组共用的规则判分边界。
- `domains/practice/batch_state.py`：公开投影与逐题辅助标记。
- `domains/assessment/models.py`：整组提交幂等记录。
- 前端 `ExerciseBatchCard.tsx`、`BatchAssessmentMessage.tsx`：整组作答和反馈。

## 验证与启用

2026-10-19 验证：后端 101 项测试通过，包含 25 项新增场景；TypeScript 检查和 Vite 生产构建通过。新增测试走真实服务、AgentRunner 和工具调用循环，使用 SQLite、脚本模型及假向量/检索服务。

实际浏览器验收使用独立 8793 端口、临时 SQLite 与脚本模型：由页面发起练习，出现单选/多选/填空三题；只完成两题时禁止提交；刷新保留两题草稿；填完第三题后统一提交，显示整组与逐题反馈，三道证据分别落库。临时服务和浏览器标签已关闭。截图保存在 `data/tmp/batch-ui-assessment.png`，不代表真实模型教学质量验收。

后端测试：

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q --basetemp=..\data\tmp\pytest-batch-final
```

本次未访问真实模型、修改正式学生数据或重启现有服务。正式启用需重启后端，现有启动初始化会自动创建新增 `batch_submissions` 表，无须修改旧表。运行中的 Vite 可读取前端源码变更。
