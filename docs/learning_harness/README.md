# 从当前项目学习 Learning Harness

当前阅读基线为 2026-10-26 PostgreSQL 迁移后的源码。LangChain 负责真实模型／工具循环与结构输出，LangGraph 负责业务分支、Send 并发、等待、记忆任务和执行恢复，领域服务继续拥有正式学习事实及三级记忆规则。

实现、实际删除和分层验收状态见[30 号记录](../stem_v1/30_native_remaining_implementation_and_acceptance.md)。N0／N1 及之前的验收保留各自历史口径，不能当作当前版本所有步骤已经通过。

## 先读主线

HTTP／SSE → 唯一学习入口 → claim 后重读正式回执与 wait → 会话 StateGraph → 标准 Agent／真实工具／必要子图 → 领域事务 → 正式消息 → 等待学生或结束。

业务表保存原答、题目、评分、目标和记忆治理指针；官方 Store 保存长期记忆正文；图和 PostgreSQL Saver 保存执行消息与未完成节点；RuntimeContext 每次重新注入身份、预算和服务。当前迁移见[31 号计划](../stem_v1/31_postgresql_langgraph_memory_migration_plan.md)。

## 文档地图

| 文档 | 问题 |
|---|---|
| [00 总览](00_overview_and_architecture.md) | 框架、领域、图与公开输出如何分工？ |
| [01 四角色](01_agent_organization.md) | 子 Agent 怎样真正执行并返回原调用者？ |
| [02 学习状态](02_learning_state_and_policy.md) | 流程、原答、局部证据和掌握估计如何区分？ |
| [03 Runtime 与上下文](03_runtime_and_context.md) | 可信运行对象、唯一消息、摘要与输入预算是什么？ |
| [04 契约与存储](04_artifacts_storage_events.md) | request／wait／operation、正式回执和 checkpoint 有什么区别？ |
| [05 学习流程](05_learning_workflows.md) | 讲解、选题、多题、评分、讲题与跳过怎样走？ |
| [06 源码阅读](06_source_reading_guide.md) | 从哪些文件和断点建立完整调用链？ |
| [07 设计取舍](07_design_principles.md) | 为什么要节点节制、短事务和正式回执？ |
| [08 Agent loop](08_agent_execution_and_tools.md) | create_agent、ToolRuntime、中间件和预算怎样协作？ |
| [09 长期记忆](09_student_learning_memory.md) | 观察如何递归归纳，纠正／遗忘怎样使旧结果失效？ |
| [10 会话恢复](10_sessions_and_context.md) | interrupt、重启、断线和安全重建分别恢复什么？ |
| [11 验证与边界](11_harness_review_and_acceptance.md) | 哪种证据支持哪种主张？ |
| [12 服务与删除审计](12_service_map_and_audit.md) | 如何判断重复机制，怎样证明旧路径已退出？ |
| [13 后端结构](13_backend_structure.md) | 六类目录、九个领域分别拥有什么？ |
| [14 依赖方向](14_dependency_direction.md) | 领域怎样使用原生图而不反向依赖应用？ |
| [15 核心元素串讲](15_core_design_elements_and_agent_loop.md) | 怎样通过一个教学例子解释完整 harness？ |

第一遍读 15→00→01→05，画出教学请求链。第二遍读 13→14→06，再选择模型／恢复／记忆专题。每个模块先问输入、输出、可变状态和失败后产物，不必逐行阅读。

本目录解释当前实现；stem_v1 保存阶段计划与验收。旧图片资源保留为历史素材，当前架构采用本文 Mermaid 和源码，不再把旧执行器图作为当前调用链。
