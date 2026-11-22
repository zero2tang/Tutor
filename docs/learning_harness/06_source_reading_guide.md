# 06 · 当前源码阅读顺序

先追踪“调用谁、能写什么、失败后保存什么”，再读局部算法。以下路径均指 N2～N6 后源码；旧注册表、独立控制器和请求内作答图已退出。

## 1. 第一遍：建立完整业务链

| 顺序 | 文件 | 要回答的问题 |
|---|---|---|
| 1 | [bootstrap.py](../../backend/app/bootstrap.py) | 模型、工具、领域服务、PostgreSQL Saver／Store 怎样装配？单 worker 锁在哪里？ |
| 2 | [api/routes/learning.py](../../backend/app/api/routes/learning.py)、[guidance.py](../../backend/app/api/routes/guidance.py) | HTTP body 怎样进入同一 execute？ |
| 3 | [entry.py](../../backend/app/application/learning/entry.py) | claim 后重读什么？回放、过时 wait、来源失效怎样处理？ |
| 4 | [graph.py](../../backend/app/application/learning/graph.py) | 七类节点如何分支，哪些字段需要 checkpoint？ |
| 5 | [publication.py](../../backend/app/application/learning/publication.py)、[receipts.py](../../backend/app/application/learning/receipts.py) | 正式回复、评分反馈和 completed 回执分别在哪个事务产生？ |

## 2. 第二遍：追踪主模型与真实工具

[planning.py](../../backend/app/application/learning/planning.py)
→ [runner.py](../../backend/app/application/agent/runner.py)
→ [角色工厂](../../backend/app/application/agent/graph.py)
→ LangChain 模型／工具节点
→ [tools.py](../../backend/app/application/agent/tools.py)
→ [delegation.py](../../backend/app/application/learning/delegation.py)
→ 原调用方继续。

重点观察 ToolRuntime.context、完整 config、parent_run_id／root_run_id、根预算和稳定操作身份。工具名称只是模型协议；真正业务执行是工具函数调用领域服务，不再查字符串注册表。

## 3. 第三遍：选择一个业务深入

| 业务 | 入口和事实所有者 |
|---|---|
| 习题质量与并发 | [practice 子图](../../backend/app/application/learning/subgraphs/practice.py) → [生成服务](../../backend/app/domains/practice/service.py) → [复核](../../backend/app/domains/practice/reviews.py) → [整组发布](../../backend/app/domains/practice/batches.py) |
| 原答与证据 | [单题评分](../../backend/app/domains/assessment/service.py)／[整组评分](../../backend/app/domains/assessment/batch.py) → [证据](../../backend/app/domains/assessment/evidence.py) → [学生状态](../../backend/app/domains/students/service.py) |
| 主子目标 | [sessions/goals.py](../../backend/app/domains/sessions/goals.py) → 版本、活动路径、补救返回 |
| 长期记忆 | [memory/service.py](../../backend/app/domains/memory/service.py) → [concepts/service.py](../../backend/app/domains/memory/concepts/service.py) → [scopes/service.py](../../backend/app/domains/memory/scopes/service.py) |
| 检索 | [课程检索](../../backend/app/domains/courses/retrieval/service.py)与[习题检索](../../backend/app/application/question_retrieval/service.py) |

## 4. 最后读恢复与公开边界

[summary.py](../../backend/app/application/agent/summary.py)解释消息如何缩减；[storage.py](../../backend/app/application/learning/storage.py)解释执行状态如何持久；[api/streaming.py](../../backend/app/api/streaming.py)解释临时帧为何不是业务事实。这是三个不同问题。

建议设断点：entry 取得 owner 后、真实工具函数、评分 commit 开始、publish 提交前、await_student interrupt。一次模型请求期间数据库事务应已结束，正式写入前必须重检。

## 5. 用故障测试建立理解

先看[原生会话测试](../../backend/tests/test_native_session_graph.py)、[习题图测试](../../backend/tests/test_native_practice_graph.py)和[跨进程父子图测试](../../backend/tests/test_native_child_restart.py)。从断点判断：工具效果已提交但父图失败时，哪一份正式回执避免再次生成或评分？

测试中的录制标准模型仅替代 provider；框架工具调用、消息回填、图节点和 SQLite 仍真实运行。真实模型和实际业务库验收位置以[实施记录](../stem_v1/30_native_remaining_implementation_and_acceptance.md)为准。
