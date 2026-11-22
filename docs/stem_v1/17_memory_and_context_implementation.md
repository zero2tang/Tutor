# 学生记忆 会话目标与上下文压缩实现

> 版本提示：本篇是历史实现记录。当前分层记忆、逐点评分与真实验收见[19 文档](19_business_memory_implementation.md)，原生会话与压缩实现见[30 文档](30_native_remaining_implementation_and_acceptance.md)。下文“当前”限定于本篇原版本，已退出文件保留路径文字供历史核对。

更新日期：2026-10-21。实现范围对应[实施计划](16_memory_and_context_plan.md)。本页解释当前代码链路；真实验收结果以本文末尾及其报告为准。

## 一 当前业务边界

跨会话只有两类学生记忆：每个学生一份短学生记忆，每个会话一份可更新的学习经历。二者的内容均通过实际模型调用提取和整合，服务负责范围、来源、长度、幂等和版本提交。

目标与子目标只属于本会话。新会话可以读取旧经历，但独立建立根目标，不自动激活旧目标。原始消息、真实作答、帮助快照与评估证据继续保留，压缩不会改写掌握状态。

## 二 会话目标的调用链

```text
SessionService.create
  → 同事务创建 SessionGoalState 根节点
LearningPlanner.plan
  → 把当前路径 依赖及其他节点短索引交给主 Agent
  → 模型可输出 Plan.session_goal_update
  → 校验本会话来源 目标版本 父子关系和依赖无环
  → 与原计划和 continuation 在同一事务保存
SessionService.get
  → 返回 session_goals，刷新后仍能恢复
```

[sessions/goals.py](../../backend/app/domains/sessions/goals.py) 分别实现节点更新、补救压栈和恢复。返回点保存原目标、原题指纹、当前步骤与下一步；恢复时验证原题没有被替换。模型不能提交自造返回栈，不能把终止节点重新激活。

分步方程和引导讲题绕过主规划器，因此在各自结果事务中调用 `sync_workflow()`。例如去括号错误触发负数乘法补问时，原题目标 blocked，补问成为当前节点；补问结束后返回原等式和原步骤。这里只记录教学活动，补问完成不产生掌握度升级。

主 Agent 和知识 Agent 可以通过 `get_session_goal(goal_id)` 回读短索引对应的完整节点。学生、课程和会话范围来自服务端 `ToolContext`。

## 三 两类模型记忆

### 数据与长度

| 对象 | 保存方式 | 输入正文约束 |
|---|---|---|
| StudentMemory | student_id 主键，一份当前有效正文 | 目标600 Tokens，上限800，最多八个要点 |
| SessionExperience | session_id 主键，一会话一份 | 目标1200 Tokens，上限2000，provisional 或 final |
| MemoryFact | 会话单调序号，来源唯一约束 | 真实已提交事实，不以摘要代替 |
| MemoryJob | 来源水位与规则版本唯一任务 | 状态 租约 重试 次数和累计模型用量 |

记忆长度采用保守估算，不能等同于模型服务商精确分词。来源和版本是维护元数据，也计入模型输入预算。

语义抽检已发现边界：即使提示词要求排除临时状态，实际模型仍曾把“尚无独立作答记录，掌握情况待观察”放入长期正文。长度、来源和版本检查通过不能证明长期与经历内容分层正确。当前专门纠正与遗忘入口可修复正文；尚未增加逐条长期候选的独立模型审查，不能宣称已保证自动提取的语义质量。

### 提取与提交

```text
原始消息和事实同事务保存
  → 完整业务结束后 capture
  → 五轮或约3000新增Tokens，或业务阶段优先事件
  → 登记持久 MemoryJob
  → 后台 MemoryWorker 领取任务
  → MemoryExtractor 读取旧记忆和未覆盖事实，分块调用模型
  → 校验来源 正文上限 知识点归属和版本
  → CAS更新两类记忆，推进 covered_seq
```

补救返回、显式目标变更、目标活动完成和讲题阶段结束会优先登记更新。空闲三十分钟只刷新 provisional；显式结束才检查结束版本后标记 final。后台失败不回滚已提交答案，进程重启恢复已有任务与已启用会话的水位，不在启动时全库自动发起模型提取。

教学轮次占用属于调度等待，任务延后而不消耗模型失败重试次数；领取后发生的占用竞争同样退回未使用额度。较新任务提交成功后，服务按提取版本、事实水位及结束版本检查旧任务，能够证明已覆盖的标记为 `superseded`，不继续无效重试。实际模型或候选失败仍采用有限退避，不能把所有冲突都视为成功。

模型可以返回 `no_change`。同义改写不会被强制保存为新记忆；超长或来源错误候选仅允许一次定向重写。模型期间不持有长数据库事务；同一学生跨会话写入和同会话经历更新分别做版本比较。

### 读取 纠正和遗忘

[MemoryService](../../backend/app/domains/memory/service.py) 提供短学生记忆和最多三份相关历史经历。历史经历先按学生、课程和知识点筛选，再限数量；短学生正文整体读取，课程相关结论在正文限定范围。

工作台可整理、纠正或遗忘记忆。遗忘会使正文与相关检查点失效，并保存最小来源撤销屏障，防止旧材料重新提取后复活。原始作答继续保留。当前专门维护入口与 API 支持这些操作，普通聊天尚未增加独立的记忆管理工具。

```text
GET    /api/sessions/{id}/memory
POST   /api/sessions/{id}/memory/refresh?wait=true
POST   /api/sessions/{id}/memory/close?wait=true
POST   /api/sessions/{id}/memory/correct?wait=true
DELETE /api/sessions/{id}/memory/{student|experience|all}
```

API 不接受可替换归属的 student_id。当前系统仍沿用本地身份模式，这些范围检查不等同于已经实现多用户登录认证。

## 四 上下文构建与压缩

### 每次模型调用都检查

当时的 `agent/runtime/runner.py` 为各入口注入本会话目标与两类记忆；[ContextBuilder](../../backend/app/agent/context/builder.py) 将当前任务固定字段与可摘要历史分开。规划器和教学委派不再先把每条消息截到1800字符。

当时的 `agent/runtime/loop.py` 在首轮、工具回执之后和 JSON 修复轮之前执行预算控制。[模型客户端](../../backend/app/infrastructure/clients/llm.py) 在 HTTP 请求发送前再次守卫，记忆提取和摘要器的直接调用也受约束。

系统规则、工具与 Schema、原题、当前学生原话、提交前帮助条件、待答问题和目标路径计入输入。历史对话、旧教学分析、旧证据和可选经历可进入摘要；重复视图不重复计入完整原文。原始数据及可审计来源仍保存在数据库。

### 预算与实际预测

可用输入预算为：

\[
H=\min(P,C-O-S),\qquad C\le W.
\]

其中 W 是配置窗口，P 是可选的提供方输入上限，C 是工程总容量，O 是本次输出预留，S 是安全量。默认 C 与 W 配置为32768、S为2048；这些是保守的部署配置，不代表程序自动查证了服务商窗口。

| 判断 | 当前规则 |
|---|---|
| 主动压缩候选 | 输入达到0.70H，且可回收历史达到 max(1024,0.15T) |
| 必须处理增长 | T加下一批预计增长G达到0.90H |
| 常规回落目标 | 0.55H，不能为凑比例删除当前任务 |
| 发送守卫 | 最终请求超输入预算则拒发 |
| 重试边界 | 同一历史来源最多两次摘要尝试，超限不盲重试原载荷 |

Token 估算器为 `weighted_unicode_v1`，ASCII按每字符0.5、非ASCII按每字符2估算；视觉输入使用配置的每图预留。当前并非部署模型精确 tokenizer，视觉预留也尚未按真实题图样本校准。事件记录估算值及服务商实际用量，便于后续调整。

G 目前采用可配置的冷启动工具增长量和已完成批次的指数平滑预测，新增批次权重为0.1。它不是已训练的预测模型，也不是P95或绝对上界；每次新输入到来后重新计数。

### 安全点与回退

当前在下一次模型调用前同步摘要已经提交的历史前缀，实际替换只发生在两次模型调用之间，尚未实现独立的异步预摘要任务。工具调用和对应全部回执作为闭合单元处理；尚未确认效果的工具不因摘要而重跑。等待学生作答是可用切点，原题、待答ID和帮助条件保持完整。

摘要由模型输出带来源的结构化结果，记录阶段、已确认结论、不确定性及未决问题。超长前缀分成最多24个来源一块，再进行有限层次合并；摘要调用本身也检查预算。检查点记录源指纹、角色、规则版本和会话版本。

每次摘要只向模型提供本次顶层来源的短别名，如 `s0`、`s1`；程序要求别名完整、唯一且没有额外项，通过后映回持久来源ID。合并阶段只校验阶段别名，原始来源映射留在服务端，避免模型混淆块编号和块内引用。服务商明确标记输出截断的候选也会被拒绝。该协议检查覆盖范围，不证明每条事实都被忠实总结。

摘要无效或超时保留旧视图。压力仍大时先将可选旧记录移入摘要，必要时降低本次输出预留并记录 requested/effective 值，再关闭新的工具请求。固定任务本身仍不适合窗口时明确要求分段，不截断当前学生答案继续评分。

## 五 模块与运维入口

| 模块 | 职责 |
|---|---|
| [sessions/goals.py](../../backend/app/domains/sessions/goals.py) | 目标结构 来源 状态 版本和返回点 |
| [domains/memory/recording/facts.py](../../backend/app/domains/memory/recording/facts.py) | 事实水位和同事务消息登记 |
| `domains/memory/extractor.py`（历史路径） | 模型提取 整合 分块和短正文校验 |
| [domains/memory/service.py](../../backend/app/domains/memory/service.py) | 两类记忆读取 CAS提交 纠正与撤销 |
| [domains/memory/recording/jobs.py](../../backend/app/domains/memory/recording/jobs.py) | 持久任务领取 有限重试 空闲刷新和恢复 |
| [shared/llm_budget.py](../../backend/app/shared/llm_budget.py) | Token估算 配置预算和最终守卫 |
| `agent/context/compactor.py`（已退出） | 压缩策略执行 模型摘要和输入重建 |
| `application/agent/checkpoints.py`（已退出） | 来源 指纹 版本与检查点保存 |

常用配置：`TUTOR_CONTEXT_REQUEST_TOKENS`、`TUTOR_CONTEXT_WINDOW_TOKENS`、`TUTOR_CONTEXT_INPUT_LIMIT_TOKENS`、`TUTOR_CONTEXT_SAFETY_TOKENS`、`TUTOR_CONTEXT_SOFT_RATIO`、`TUTOR_CONTEXT_PREVENTIVE_RATIO`、`TUTOR_CONTEXT_TARGET_RATIO`、`TUTOR_CONTEXT_KEEP_RECENT_TURNS`、`TUTOR_CONTEXT_COMPRESSION_ENABLED`。

事件 `memory_context_loaded` 记录进入构建器的记忆版本及经历ID，不记录正文；`context_budget` 记录压前后输入、触发原因、有效输出预算和耗时；`model_request_completed` 记录模型实际用量。后台记忆用量单独保存在 MemoryJob，汇总成本时不能漏掉。

## 六 验证与实测边界

最终冻结后端源码指纹为 `65634cc23e412a586ea2176290676e934dc8d3c2692d7c0b5a3ebc183e2f61a0`，与两份真实验收报告前后记录一致。

| 验证 | 本次结果 |
|---|---|
| 后端全量回归 | 552通过，0失败；两个隔离测试组253与299项，并行约226秒 |
| 前端 | TypeScript与Vite构建通过，15项流式与恢复测试通过 |
| [真实主流程报告](../../data/reports/memory_context_acceptance.json) | 5/5通过，50次HTTP均为200，11次Agent运行均完成 |
| 目标与记忆 | 补救后返回原题原步骤；新会话独立目标；实际模型生成两类记忆并跨会话加载；重放未新增消息、事实和运行 |
| 长文本压力 | 同题7轮，其中6段约2400字模拟思考；8次有效压缩、8份检查点，原始来源、提交原话和帮助条件保持完整 |
| 后台任务终态 | 本次9个任务：8个completed、1个superseded，无失败；领取尝试合计8次，正常占用延后单独计数 |
| [真实维护报告](../../data/reports/memory_maintenance_acceptance.json) | 4/4通过；实际模型纠正偏好、撤销两类正文；原始消息、辅导证据和学生状态指纹不变 |

真实主流程使用独立MySQL库 `stem_tutor_memory_acceptance`、8011端口与配置的 `deepseek-flash`。为在有限轮次内触发压力，本次软水位改为0.20、回落目标0.15、近期保留1轮；窗口与请求容量仍为32768，预防线仍为0.90。正常运行默认仍是0.70、0.55、4轮，不能把压力参数视为已调优默认值。

最后一次压缩的估算输入由83101降至25076，有效输出预留2250，输入预算28470；同时关闭新工具调用。固定任务本身不能压到回落目标时保留完整任务。该单次投影变化不是总费用节约比例，估算Token也不等同于服务商实际用量。

主流程实际返回用量：Agent输入214278、输出16098 Tokens，已含语义压缩；后台记忆31次逻辑模型调用，输入65003、输出21093 Tokens。记忆维护另有2次逻辑调用，输入2296、输出598 Tokens。已观测教学HTTP尝试17次、重试0；记忆提取没有逐HTTP尝试的同等指标，不能用逻辑调用数声称覆盖所有网络重试。当前样本没有记忆usage缺失记录。

本次还修正了验收脚本本身的错误：首次讲题聊天消息按既有契约存题干，请求原话在证据 `raw_input`，原脚本误要求二者相等。全部50次HTTP完成后，对保留轨迹做了只读复核：分别验证首次题干、后续聊天原文与全部提交原文，等待后台任务终态，不新增模型调用、不修改业务库。原失败报告保留为[审计修正前报告](../../data/reports/memory_context_acceptance_before_start_input_audit_fix.json)，[最终报告](../../data/reports/memory_context_acceptance.json)含 `audit_recheck` 与原脚本指纹，未将复核冒充一次新的HTTP执行。一次性复核脚本已退役，历史源码保存在[源码档案](../../data/archives/native_remaining/20261023/ancillary_reference.zip)的 `scripts/evaluation/recheck_memory_context.py`。

更早真实压力流程暴露的重复投影、输出预留重置、多块来源混淆及占用误耗重试均已修正；失败报告保留在同目录。维护语义抽检的最终样例只保留稳定偏好并通过，但此前曾有瞬态混入的失败样例，不能据一个成功样例保证所有学生摘要质量。

复现入口为 [check_memory_context.py](../../scripts/evaluation/check_memory_context.py)、[check_memory_maintenance.py](../../scripts/evaluation/check_memory_maintenance.py)，只读审计为 [memory_context_audit.py](../../scripts/evaluation/memory_context_audit.py)。在项目根目录运行：

```powershell
.\scripts\evaluation\start_memory_context_backend.ps1
.\backend\.venv\Scripts\python.exe scripts/evaluation/check_memory_context.py
.\backend\.venv\Scripts\python.exe scripts/evaluation/check_memory_maintenance.py
```

复现会创建新的模拟学生并调用实际配置模型，产生真实用量；现有报告保留了本次执行结果。专用验收服务在本轮结束后停止，主工作台使用8008端口及默认预算。

本次验收使用独立MySQL库和真实配置模型，学生回答及长文本压力情境由脚本明确模拟。它验证业务链路和工程不变量，不证明真实学生学习增益，也不形成吞吐或费用基准。

尚需后续实测：精确Token估算与图像预算校准、语义摘要关键事实的人工标注召回、默认阈值的成本和延迟优化。后台worker按当前单进程本地部署组织，不能把它表述为已完成分布式调度或任意Agent执行点恢复。
