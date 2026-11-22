# 后端源码阅读导航

当前 N2～N6 实施和真实验收见[30 号记录](../../docs/stem_v1/30_native_remaining_implementation_and_acceptance.md)。HTTP 写操作统一进入 [learning/entry.py](application/learning/entry.py)，由会话图编排；旧工具注册表、ContextController、WireChatModel 和每请求作答图已经删除。

先读 [main.py](main.py)、[bootstrap.py](bootstrap.py) 和 [API 路由表](api/router.py)，再沿业务链进入领域服务。完整阅读顺序见[源码导航](../../docs/learning_harness/06_source_reading_guide.md)。

| 目录 | 职责 | 阅读入口 |
|---|---|---|
| `api/` | 输入协议、HTTP 与临时 SSE | [learning.py](api/routes/learning.py)、[streaming.py](api/streaming.py) |
| `application/learning/` | 唯一生命周期、七类节点、正式回执和持久恢复 | [entry.py](application/learning/entry.py)、[graph.py](application/learning/graph.py)、[receipts.py](application/learning/receipts.py) |
| `application/agent/` | 标准模型与 Agent 工具循环、摘要和公开边界 | [runner.py](application/agent/runner.py)、[graph.py](application/agent/graph.py)、[tools.py](application/agent/tools.py)、[summary.py](application/agent/summary.py) |
| `domains/` | 课程、习题、评分、目标、讲题与长期记忆的业务事实和事务 | [域导航](domains/README.md) |
| `agent/` | 可信 RuntimeContext、预算、来源投影与 Skill | [contracts.py](agent/runtime/contracts.py)、[builder.py](agent/context/builder.py) |
| `infrastructure/` | 配置、标准模型、检索、图谱与数据库客户端 | [chat.py](infrastructure/clients/chat.py)、[database.py](infrastructure/persistence/database.py) |
| `shared/` | 并发辅助、稳定身份、预算和失权异常 | [identity.py](shared/identity.py)、[execution.py](shared/execution.py) |

建议沿三条链阅读：

1. **讲解与委派**：HTTP → entry → 会话图 supervisor → 唯一 Runner → `create_agent` → 真实 `@tool`／ToolRuntime → 知识或习题子 Agent → 原主模型 → publish。讲题活动的已发布原答和公开回复以业务 ID 增量进入同一父 State。
2. **作答与证据**：entry → prepare_assessment → 标准 JSON mode 结构化模型 → commit_assessment → [评分证据](domains/assessment/evidence.py)与[掌握度归约](domains/students/mastery.py) → supervisor 接续。原答、帮助条件、Attempt 和证据的正式准入由事务校验。
3. **并行生成**：[Send 题组图](application/learning/subgraphs/practice.py) → 共用 draft／blind_solve／review → [生成回执](domains/practice/service.py) → [整组发布](domains/practice/batches.py)。节点用于并发、有限重试和恢复；普通校验继续是函数。

PostgreSQL 业务表保存教学事实、目标和记忆治理指针；官方 Store 保存不可变记忆正文，官方 PostgreSQL saver 保存执行消息、位置和停点；授权与当前 owner 每次推进重新注入。记忆任务的生成、验证、复核、预写和发布由独立 LangGraph 执行。数据库单 worker 锁及 30 天冷线程清理见 [storage.py](application/learning/storage.py)。

标准 `create_agent` 使用真实工具与 ToolStrategy；无业务工具的评分、盲解、复核、摘要及后台记忆统一使用 `with_structured_output(method="json_mode")`，自动注入类型 Schema 并严格校验原始 JSON。没有旧聊天调用回退。

静态依赖遵循 `api → application → domains → agent/infrastructure → shared`。`agent` 与 `infrastructure` 互不导入；运行时服务回调不构成源码反向依赖。Skill 是教学方法文本，不能替代权限和事务校验。
