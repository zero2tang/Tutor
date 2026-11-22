# Tutor PostgreSQL 与 LangGraph 记忆架构迁移计划

更新日期：2026-10-26。状态：P0～P4 已完成。最终实现与证据见[32 号验收记录](32_postgresql_langgraph_memory_implementation_and_acceptance.md)。

## 目标与冻结契约

本次只迁移数据库和工程架构，知识点 → 课程 → 学生总体三层记忆的设计逻辑、教学行为和公开接口不变。保留教学资源；旧学生记录、会话、作答、评估和记忆不导入新系统，完整备份保留。允许停机维护，一次切换，不建设双写。保留 ES、Neo4j、检索方式、模型配置、FastAPI、本地单 worker 和当前 LangChain/LangGraph 版本。

冻结下列行为：

- 课程模式要求多个独立任务及至少两个知识点；总体模式要求多个独立任务及至少两门课程。长期偏好按原作用域授权，上层来源过滤和独立复核保留。
- 自述、讲过、提示后答对不升级为独立掌握证据，记忆不回写正式判分。
- 纠正、遗忘、暂停/恢复、父级依赖失效、经历 provisional/final、实时撤销校验和待答隐藏规则保留。
- 普通完成轮累计至少五条用户消息事实或 3000 估算 Tokens；目标/教学阶段变化强制触发；后台每两秒检查任务，每分钟检查一次超过 30 分钟的空闲会话；手动刷新/关闭保留。
- 活跃教学轮延期，不消耗调用预算；后台记忆不阻塞教学响应；上层失败不重跑已成功叶子。
- 记忆注入不超过可用输入的 20% 且最多 4000 Tokens，最多三个相关知识点；评分隔离、目标/子目标和完整当前任务保护不变。
- 正文版本、来源代次、撤销代次、关闭版本、工作流版本保持独立含义。

## 目标架构

| 职责 | 承载方式 |
|---|---|
| 正式业务事实 | PostgreSQL、SQLAlchemy、Alembic |
| 会话及记忆任务执行状态 | LangGraph、AsyncPostgresSaver |
| 会话观察及三级长期记忆文档 | 官方 PostgreSQL Store、AsyncPostgresStore |
| 提取/整合/验证/复核/发布 | 独立 StateGraph |
| 唤醒、执行权、跨重启任务尝试预算 | 精简持久任务记录 |
| 教学资源检索 | 现有 ES 与 Neo4j |

每个逻辑任务有稳定图线程，按 session_extract、知识点、课程、学生任务路由：冻结来源 → 提取/整合 → 业务验证 → 上层复核（适用时）→ 发布 → 登记父层更新。候选和节点进度由 Checkpointer 保存，关键节点同步持久化。会话/知识点持久任务尝试上限五次，课程/学生三次；RetryPolicy 负责节点内退避；模型分块、有限修复和独立复核调用单独计量，不将任务尝试次数冒充实际模型调用数。跨重启领取仍受持久任务预算约束。

Store 保存不可变版本的正文、claims、来源及依赖。scope_memories 与 session_memory_extractions 保留各自治理职责与已发布指针，不强制合并。文档先写，短事务重新验证执行权、来源和撤销代次后发布指针、回执及父层任务。读取只认已发布指针，孤儿文档不可见；重放依发布回执幂等，不能直接修改框架内部表拼接事务。

命名空间从服务器归属推导，不向模型暴露通用 Store 写入口。Store 不启用向量索引，不新增 pgvector。保留 create_agent、现有摘要中间件、记忆/API/SSE 对外结构和 request_id/wait_id/question_id。

## 实施阶段

| 阶段 | 状态 | 交付与验收 |
|---|---|---|
| P0 计划与基线 | 已完成 | 源码、未跟踪文件、工作区补丁和清单已归档；规则 AST 对照通过 |
| P1 PostgreSQL 基础设施 | 已完成 | Alembic 20261026_02、依赖锁、官方 Saver/Store、启动检查、PG 进程恢复通过 |
| P2 资源迁移 | 已完成 | 新完整快照 27 表 16,309 行；资源 13 表 13,493 行规范化哈希通过，旧学生数据未导入 |
| P3 框架记忆 | 已完成 | 七节点 StateGraph、异步 Store 预写与治理指针、恢复与持久预算测试通过 |
| P4 验证与切换 | 已完成 | 后端 865 项，前端 15 项及构建，真实模型、连续摘要和检索回放通过；前后端已启动 |

P0 源码备份：data/archives/postgresql_memory_migration/20261026_162109/，共 492 个文件。原源码及未提交修改不重置。

### P1 数据库与启动

同步依赖和锁文件，沿用当前已安装版本；MySQL 驱动仅用于离线工具。业务结构由 Alembic 管理，官方 Saver/Store 用 setup 管理。已有空表只有与基线结构完全匹配才能 stamp，否则停止。生产启动只检查版本，建库、种子导入、历史修复改显式维护命令。保持 UTC naive 与微秒精度，替换 SQLite 文件锁、冷线程统计和清理。

### P2 资源白名单

全量迁移 courses、concepts、questions、materials、concept_relations、textbook_documents、textbook_import_receipts、textbook_extraction_sections、exercise_search_assets、exercise_search_representations、exercise_search_projections。

index_tasks 只迁 target 为 es、neo4j、question_search 的资源任务；model_calls 只迁教材处理，以及 purpose=question_search 且 stage=extract 的题图提取。当前旧快照参考为 13 表、13,493 行，切换以新快照为准。全部题目保留，50 道历史生成题继续 can_search=False，不将旧会话运行记录导入。

其余旧学生、运行、作答、证据与记忆数据不导入。切断学生页面对旧 student_summaries.json 的在线兜底，原档案保留。新状态应显示未评估，不制造零分。

迁移分导出、预检、导入、验证；先验证文件与筛选闭包，再向业务数据全空目标单事务导入。Boolean/时间/JSON 按目标类型规范化后算哈希，保留原快照哈希；任一不一致全部回滚。非空目标只允许 verify，禁止自动补写。保留资源 ID、权限、ES 投影及 namespace/generation/别名，保留 PDF/图片/裁剪/哈希/缓存，不重做嵌入。

### P3 记忆重构

沿用领域校验器和提示词。图负责候选与执行恢复，持久调度表负责触发、到期和执行权，控制表负责事务治理。候选存检查点后再发布；纠正/遗忘等治理也须经同一存储入口。删除被替代的正文双写、result_snapshot 和重复执行状态机，保留独立故障测试，不以表数作为结果。

### P4 切换与回退

维护窗口暂停写入和资源构建，保存新的完整源快照，完成资源导入、校验及回归后启动服务。检查实际进程、健康状态、OpenAPI、新会话、后台记忆、ES/Neo4j。原 MySQL、SQLite 与备份保留。新 PG 已产生写入后，回退必须先保全新数据，不能直接切回旧库。

## 验收与证据

- 行数、规范化哈希、资源引用、资产文件一致；损坏快照和非空目标拒绝；新页面无旧学生记录。
- 现有三级记忆业务断言、触发、汇总、偏好、纠正遗忘、暂停恢复、公开投影通过。
- 覆盖来源提交后崩溃、Store 后崩溃、发布后 checkpoint 失败、上层独立恢复、跨会话竞争、遗忘期间迟到写入、重启不重置预算。
- 正式评分隔离、目标/子目标压缩保留、作答重放幂等、ES/Neo4j 固定回放与答案隔离通过。
- 运行完整后端、前端测试/构建、真实 PG 重启恢复及固定素材真实模型链路；报告框架表数量、行数与存储开销。
- 生产业务/检查点使用 PostgreSQL，三级记忆真实经过 Store 和任务图，旧路径退出，文档与验收回执同步。历史测试成绩不替代本轮结果。

官方依据：[持久化](https://docs.langchain.com/oss/python/langgraph/persistence)、[PostgreSQL 记忆](https://docs.langchain.com/oss/python/langgraph/add-memory)、[摘要中间件](https://docs.langchain.com/oss/python/langchain/short-term-memory)。
