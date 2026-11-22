# 00 · 当前原生架构总览

本篇的执行图基线来自 2026-10-23 N2～N6；2026-10-26 已接入 PostgreSQL 业务表、官方 Saver／Store 和记忆任务图。迁移范围见[31 号计划](../stem_v1/31_postgresql_langgraph_memory_migration_plan.md)，历史原生化验收见[30 号记录](../stem_v1/30_native_remaining_implementation_and_acceptance.md)。

## 1. 从一次学生输入看边界

```text
HTTP / SSE → LearningService.execute → entry.execute
  → 协议校验、claim、正式回执、wait、目标与记忆版本核对
  → 会话 StateGraph
  → supervisor / assessment / guidance / 显式选题与跳过
  → 领域事务与正式消息
  → 等待学生 interrupt，或结束本轮
```

[会话图](../../backend/app/application/learning/graph.py)只有七类节点。外层图选择业务分支，supervisor 内的标准 LangChain Agent 决定检索和教学工具；没有第二个 LLM router。

| 组件 | 解决的问题 | 不能替代的责任 |
|---|---|---|
| LangChain create_agent／ToolStrategy | 模型—真实工具—ToolMessage—模型的循环和结构输出 | 教学正确性、课程范围和正式数据库授权 |
| LangGraph StateGraph／Send | 分支、并发、节点恢复与等待点 | 知识点关系图、评分事务 |
| PostgreSQL checkpointer | 当前执行消息、未完成节点、候选与等待栈 | 正式原答、题目、掌握证据和长期记忆发布权限 |
| PostgreSQL Store | 不可变的观察及三级记忆正文 | 作用域授权、来源校验、撤销与 SQL 发布事务 |
| 领域服务 | 校验事实、版本与帮助条件，短事务提交 | 模型自由教学决策 |
| SSE | 允许公开的增量文字与执行进度 | 持久消息总线、逐 token 恢复 |

## 2. 三类状态分别由谁拥有

正式学习事实及记忆治理状态属于 PostgreSQL 业务表，记忆正文属于官方 Store，模型执行状态属于原生图的 PostgreSQL Saver。身份、当前执行权、服务对象、预算与校验器属于每次重新注入的 RuntimeContext，不能从旧 checkpoint 恢复授权。

会话图持有跨回合 messages。角色 Agent 在标准图内使用消息并调用真实工具，成功后通过 Overwrite 把结果接回父图，传播摘要删除。并行习题分支只返回产物和回执，不争写父消息。

## 3. 六项核心能力的位置

- 递归记忆：事实／观察→知识点→课程→学生，继续由[记忆域](../../backend/app/domains/memory/service.py)拥有。
- GraphRAG：[课程检索](../../backend/app/domains/courses/retrieval/service.py)使用知识关系和有限材料召回，Neo4j 没有被执行图替换。
- 会话主子目标：[目标服务](../../backend/app/domains/sessions/goals.py)负责版本、活动路径和补救返回。
- 并行习题：[质量子图](../../backend/app/application/learning/subgraphs/practice.py)共用生成／盲解／复核，整组再统一发布。
- 长对话：[摘要中间件](../../backend/app/application/agent/summary.py)压缩闭合历史，保留完整当前任务。
- SSE：[传输层](../../backend/app/api/streaming.py)保持有界临时队列和断线后台完成。

## 4. 先读哪些文件

先读[唯一入口](../../backend/app/application/learning/entry.py)和[七节点图](../../backend/app/application/learning/graph.py)，再读[角色工厂](../../backend/app/application/agent/graph.py)、[真实工具](../../backend/app/application/agent/tools.py)与[正式回执](../../backend/app/application/learning/receipts.py)。[15 章](15_core_design_elements_and_agent_loop.md)把这些组件串成一个教学例子；[06 章](06_source_reading_guide.md)给出断点顺序。

旧 WireChatModel、工具注册表分派、独立 ContextController 和请求内作答图已经退出。当前仍保留的是教学保护和明确的业务事务，不是另一套后备执行器。
