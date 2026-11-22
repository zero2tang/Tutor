# 03 · Runtime、消息与上下文

本篇解释当前一次角色调用怎样取得可信能力，以及长历史怎样进入标准 Agent。入口是[runner.py](../../backend/app/application/agent/runner.py)，不再存在独立 ContextController。

## 1. 运行对象与持久 State 分开

| 对象 | 内容 | 生命周期 |
|---|---|---|
| ToolContext | 课程、学生、会话、目标知识点、排除题和请求 ID | 服务端构建的业务范围 |
| RuntimeContext | 真实角色／父子 run、预算、工具、展示器、来源校验器 | 每次调用重新注入，不作为 checkpoint 授权 |
| TeachingState | messages、input_event、评分候选、结果、停点、目标／记忆版本 | 原生会话图持久状态 |
| PreparedRun | 本次提示、输入投影、用途、公开字段、图片读取器 | 角色调用前的准备结果 |

[TeachingRunPolicy](../../backend/app/application/agent/policy.py)负责教学上下文与记忆来源；[RuntimeContext](../../backend/app/agent/runtime/contracts.py)只承载运行依赖。图恢复必须重新取得 owner，旧 turn_id 或服务对象不能被恢复为有效权限。

## 2. 唯一消息状态怎样流转

首次缺少执行历史时，task_messages 从正式记录建立历史单元，并追加稳定 ID 的当前任务；已有 messages 时，不重复注入整份 recent_messages 和已完成委派正文。

角色工厂使用标准 create_agent，真实工具结果由框架写入 ToolMessage。主 Agent 返回后，父会话图通过 Overwrite 接回完整消息结果；不能把它再当作一批增量追加，否则被摘要移除的前缀可能复活。

子 Agent 的运行上下文默认按调用隔离，同时以 checkpointer=None 继承父 saver。隔离上下文与调用内持久恢复并不矛盾。纯结构化评分不持有另一份会话执行历史；父习题质量图只保存候选及调用回执。

## 3. 摘要中间件的职责

[TeachingSummaryMiddleware](../../backend/app/application/agent/summary.py)实际挂载在 Agent 生命周期中。它按模型输入预算触发，只总结可闭合的旧消息组，当前完整任务受保护。

自定义部分保留真实业务保护：来源短别名完整覆盖、分块摘要、跨块归并、工具组闭合、摘要确有缩减、记忆来源失效。摘要不能修改原题、学生原答、目标、权限或掌握状态；这些仍以领域事实为准。

当固定任务连同工具 Schema 已超安全预算时，系统拒绝发送，不能静默截断原答。缺少有效摘要时也不能靠一条“compressed”事件证明成功；[只读审计](../../scripts/evaluation/memory_context_audit.py)检查实际持久摘要和来源变化。

## 4. 输入预算的数学边界

设部署可用窗口为 \(W\)，单请求限制为 \(C\)，输出预留为 \(O\)，安全量为 \(S\)，额外输入上限为 \(I\)。[RequestBudget](../../backend/app/shared/llm_budget.py)采用：

\[
B_{\mathrm{input}}=\min(I,\ C-O-S)
\]

未设置 \(I\) 时取 \(C-O-S\)，配置同时要求 \(C\leq W\) 且 \(C-O-S>0\)。文本、图片预留和真实工具声明共同占用预算。这里使用本地保守估算，不冒充 provider 精确分词；实际 usage 缺失时记录未知。

## 5. 图片和 Skill 如何接入

[图片上下文](../../backend/app/application/agent/images.py)检查题图归属、格式、大小与去重，将真实允许的图片交给标准多模态消息。图片像素按本轮读取，不永久复制进会话执行历史，解析配图不会自动混入待答公开题面。

Skill Markdown 由[技能库](../../backend/app/agent/runtime/skills.py)按角色读取。它提供专业方法，不授予额外课程、学生或提交权限。权限和故障边界仍由真实工具、中间件与领域服务执行。

## 6. 怎样观察失败

模型错误、取消、执行权丢失、来源失效和普通参数拒绝分别处理。失权及取消必须退出，不得转成供模型继续修复的成功回执。AgentRun 保存已知用量和状态；SSE 展示的是允许公开的正文与进度，不公开内部答案。
