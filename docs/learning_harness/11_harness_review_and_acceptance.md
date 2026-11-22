# 11 · 验证方式与能力边界

判断当前系统要区分源码结构、隔离执行、真实 provider 和实际数据库切换。2026-10-26 PostgreSQL 迁移按[31 号计划](../stem_v1/31_postgresql_langgraph_memory_migration_plan.md)执行，本次完整后端、真实 Store／Saver、真实三级记忆、连续压缩与检索证据位于 data/reports/postgresql_memory_migration/。以下原生化验收记录属于 2026-10-23 的历史阶段，不改写为新版本通过。

## 1. 已有证据的口径

2026-10-23 阶段最终完整后端运行 **831 passed、214.44 秒**，对应[backend_complete.xml](../../data/reports/native_remaining/backend_complete.xml)，包含当时撤销、owner、消息顺序和讲题返回上下文用例。早期 806 项快照及失败报告继续保存，不与本次数量相加。

[test_native_child_restart.py](../../backend/tests/test_native_child_restart.py)的两个用例在新 Python 进程重建实际 create_agent 与父子图，使用真实 SQLite 与正式工具回执；模型端为录制标准 ChatModel，证明框架恢复协议，不能当成真实 provider 验收。

真实 DeepSeek 知识委派返回原主模型、实际 SSE 正文分块、连续两轮摘要与重开 SQLite 已有报告。摘要使用夹具历史，验收进程暂调软水位，生产阈值未改；它不证明任意真实长对话的语义零遗漏。

实际业务库已备份并转换：MySQL 28→27 表，214 条完成回执、20 条委派回执、9 个工作区、92 条观察来源和19条公开等待回执保持原业务 ID 与原始证据。真实题组已用原请求恢复提交2条 Attempt／1条 BatchSubmission，重放无模型调用；真实三层记忆与已有图种子检索通过。前端15项及构建通过。复杂纯评分使用标准JSON mode严格校验，旧函数模式失败和修复历史完整保留。

## 2. 关键主张对应什么测试

| 主张 | 入口 |
|---|---|
| v2 输入及等待点拒绝 | [native request](../../backend/tests/test_native_request_protocol.py)、[session edge](../../backend/tests/test_native_session_edge_cases.py) |
| 正式评分与安全接续 | [session graph](../../backend/tests/test_native_session_graph.py)、[session edge](../../backend/tests/test_native_session_edge_cases.py) |
| 习题 Send／质量／幂等／取消 | [native practice](../../backend/tests/test_native_practice_graph.py)、[semantic reviews](../../backend/tests/test_exercise_reviews.py) |
| 原题、帮助与跨会话隔离 | [exercise context](../../backend/tests/test_exercise_context.py)、[concept evidence](../../backend/tests/test_concept_evidence.py) |
| 原生存储生命周期 | [native storage](../../backend/tests/test_native_storage.py) |
| 摘要真实来源与只读审计 | [native memory audit](../../backend/tests/test_native_memory_audit.py)、[context compaction](../../backend/tests/test_context_compaction.py) |
| 单向依赖与路径 | [dependencies](../../backend/tests/test_backend_dependencies.py)、[layout](../../backend/tests/test_backend_layout.py) |

旧生产 answer_graph.py 及对应旧名称测试已退出；评分和安全接续通过当前会话图协议验收。

## 3. 怎样复现隔离回归

从 backend 执行 python -m pytest tests -q，并选择工作区内新的 --basetemp 目录。Windows 沙箱可能阻止 asyncio 本地 socketpair 初始化；需区分环境阻塞与业务失败，不能把卡住的进程误报为测试通过。

标准 SDK／MockTransport 测试验证请求格式和协议；录制 ChatModel 测试验证工具和领域逻辑；真实模型调用会产生费用，应使用隔离验收学生和库。

## 4. 当前边界

- 本机个人、单 worker；文件锁不提供多 worker 分布式 lease。
- 当前 SQLAlchemy、Saver 和 Store 使用独立事务，靠具体幂等回执与记忆发布指针恢复，不作通用 exactly-once 承诺。
- 没有任意 token 位置恢复，也没有逐字 SSE 续传。
- 活跃 checkpoint 历史没有硬上限；冷线程删除不保证立即缩小物理文件。
- GraphRAG 保留知识结构能力，未因框架迁移自动获得更好的召回或教学质量。
- 多次模型审查、启发式掌握估计和诊断置信度都需要独立效果评估。

## 5. 面试时如何展示证据

选一个具体故障：题目已经正式提交，父 Agent 尚未完成时进程退出。展示新的执行权、同一操作身份、回放正式题目和原父模型继续读取工具回执。然后说明哪些字段归数据库、哪些归图、哪些必须重注入。机制解释比只说“用了 LangGraph”更有说服力。
