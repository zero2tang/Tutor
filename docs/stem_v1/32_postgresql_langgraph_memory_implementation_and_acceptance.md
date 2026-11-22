# PostgreSQL 与 LangGraph 记忆架构迁移：实施与验收

更新日期：2026-10-26。状态：P0～P4 已完成，前后端已在本机启动。本轮按[31 号计划](31_postgresql_langgraph_memory_migration_plan.md)迁移数据库与工程架构，保留知识点 → 课程 → 学生总体三级记忆规则、公开接口、ES 与 Neo4j。

## 交付结果

- PostgreSQL 业务结构由 Alembic 管理，当前版本 `20261026_02`。启动检查版本，建库、升级与官方组件初始化通过显式维护命令执行。
- 会话与记忆任务检查点使用官方 `AsyncPostgresSaver`；长期观察与三级记忆正文使用官方 `PostgresStore / AsyncPostgresStore`，未启用 pgvector。
- 每个记忆任务有稳定图线程 `memory:{job_id}`，执行冻结、生成、验证、复核、异步预写、发布和后续调度七个节点。模型候选由检查点保存，任务表保留调度、任务尝试预算和有界审计。
- 三类原记忆模型的 SQL 正文列已退出，业务表保存治理状态和 `document_key`。Store 保存带哈希的不可变文档；业务事务发布指针，读取只认已发布版本。
- 子层发布时同事务登记父层待更新任务，防止发布后断电漏掉后续汇总。已完成发布重放不递增版本；已保存候选恢复不重复调用生成模型，复核恢复不重复计量成功步骤。
- 会话／知识点任务最多五次尝试，课程／学生最多三次，跨重启保留额度；实际模型调用另含既有分块、有限修复与独立复核，按 metrics 单独计量。框架 `RetryPolicy` 负责幂等 Store 预写的短暂失败重试。
- 保留原有触发、偏好授权、来源验证、帮助条件、反例、纠正遗忘、暂停恢复及评分隔离。删除已被新链路排除的旧 `legacy_candidate` 扫描。

原有同步领域查询通过官方同步 Store 适配，图节点通过官方异步 Store 预写。同步纠正／遗忘等治理操作仍可能在 SQL flush 阶段写入 Store；各入口使用独立事务，通过不可变文档、发布指针和回执保持一致性，不构成跨连接原子事务。

## 资源迁移与存储

原 MySQL 完整快照包含 27 表、16,309 行，新备份为 `data/backups/postgres_migration/mysql_snapshot_20261026_163501.jsonl.gz`。原快照及原 MySQL 保留。

目标导入 13 张资源表，共 **13,493 行**：4 门课程、1,386 个知识点、7,982 道题、1,457 条材料、2,362 条知识关系，及教材、题图、检索投影、资源任务和技术缓存。50 道历史生成题保留内容，继续不可共享检索。14 张学生、会话、作答、评估、运行与记忆表未导入旧数据。

导出回执验证原文件哈希和原数据哈希；导入按目标类型规范化 Boolean、时间与 JSON 后比较内容哈希。预检先于目标连接，目标全表空检查和写入处于同一事务，重复导入拒绝；`verify` 为只读。资源 ID、可见性、ES 别名、generation、namespace、投影以及本地资产保留，没有重建索引或重新请求嵌入。

正式库仍为 27 张业务 ORM 表；另有 Alembic 版本表。官方检查点 schema 有 4 表，Store schema 有 2 表；导入后的框架关系存储合计 155,648 字节（含索引及迁移元数据），用户检查点与记忆文档均为 0 行。实际表级数据见[导入后存储报告](../../data/reports/postgresql_memory_migration/storage_after_import.json)。

已切断新学生页面对旧 `student_summaries.json` 的在线回填，旧档案保留。无新作答时显示未评估，掌握值为 null。

另修复了实证的查询兼容差异：原 MySQL 名称和题干列使用忽略大小写排序规则，PostgreSQL 默认 LIKE 会漏掉 X、SIN、newton 等查询。知识点名称和题干筛选现显式使用 `icontains`；原本大小写敏感的练习记录 JSON 查询保持原语义。

## 本轮验收

| 验收 | 本轮结果与证据 |
|---|---|
| 完整后端 | **865 passed，357.67 秒**，启用 `TUTOR_TEST_POSTGRES=1`；[XML](../../data/reports/postgresql_memory_migration/backend_final.xml)、[日志](../../data/reports/postgresql_memory_migration/backend_final.log) |
| 前端 | 15 项流式／恢复测试通过，TypeScript 与 Vite 生产构建通过；前端源代码与 P0 备份一致 |
| 真实 PostgreSQL | 覆盖空库升级、规范化、损坏快照、事务回滚、非空目标拒绝、冷线程保留、第二 worker 排斥、独立进程检查点恢复、同步／异步 Store 互读、撤销归属与回滚、缺失或不匹配 Store 初始化拒绝 |
| 新恢复边界 | 9 项记忆图专项通过，覆盖发布前后失败、候选恢复、复核恢复、已完成发布重放、跨重启任务额度及 Store 重试；纳入完整回归 |
| 真实模型记忆 | 独立临时 PG 库，仅发送自编素材；形成 4 个知识点、2 个课程、1 个学生总体记忆，实际 Store 指针和图检查点存在；连接关闭重开后正文一致，遗忘后旧版本不可见，未产生正式作答证据；[报告](../../data/reports/postgresql_memory_migration/real_memory.json) |
| 连续摘要 | 真实模型连续两轮摘要及 PG saver 重开通过，当前原题与原答完整保留；仅验收进程降低软水位，生产参数未改；[报告](../../data/reports/postgresql_memory_migration/real_compaction.json) |
| 检索与资源 | 16 道固定题精确查找、12 条固定查询、真实 Neo4j 扩展通过；ES 别名与文档数量、图节点与关系数量保持一致，新增模型请求为 0；[报告](../../data/reports/postgresql_memory_migration/resources.json) |
| 原规则等价性 | 原 28 个记忆模块中 22 个 AST 相同；采集、空闲触发、准入、JSON 调用、层级输入、知识点整合等选定函数 AST 全部相同；[报告](../../data/reports/postgresql_memory_migration/rule_equivalence.json) |
| 架构退出与图结构 | 旧执行路径未恢复，七节点记忆图及 SQL 正文列退出检查通过；[报告](../../data/reports/postgresql_memory_migration/architecture_audit.json) |

最终全量运行前后生产源码 SHA-256 一致：`631780654232d3143ab0ebd37e44578ee1ece0e11f89109100127edd0a146e55`。[逐文件清单](../../data/reports/postgresql_memory_migration/source_frozen.json)保存实际校验范围。中途失败和修复前的回归结果分别保留，不与最终成绩相加。真实模型记忆和摘要使用的临时数据库已清理，未把验收学习记录写进正式库。

首次检索回放曾触发线上按需登记逻辑，产生 159 条派生题图缓存；已确认无投影引用、归档后清理，原有 33 条资产保留。回放工具现显式抑制缓存登记并使用只读事务，重跑通过；切换后再次执行完整资源哈希核对，13,493 行与资源快照一致。[验收总表](../../data/reports/postgresql_memory_migration/acceptance.json)记录最终测试、源码、服务及存储状态。

这些结果验证本轮工程迁移和冻结行为，不代表长期教学效果、任意长对话语义零遗漏、全库检索质量提升或多 worker 容量结论。

## 运行、维护与恢复

前端：[http://127.0.0.1:5173](http://127.0.0.1:5173)；后端：[http://127.0.0.1:8008/docs](http://127.0.0.1:8008/docs)。实际 `/api/health` 返回 ready，PG、ES 与 Neo4j 均正常，[切换回执](../../data/reports/postgresql_memory_migration/service_cutover.json)与[健康记录](../../data/reports/postgresql_memory_migration/health.json)已保存。启动脚本使用适配 psycopg 的 Windows Selector 事件循环。

```powershell
# 初始化新部署；日常服务启动不执行这些 DDL。
backend/.venv/Scripts/python.exe scripts/maintenance/prepare_postgres.py
# 导出、预检、导入和验证分别执行；导入只接受全空业务库。
backend/.venv/Scripts/python.exe scripts/maintenance/migrate_postgres.py verify data/backups/postgres_migration/mysql_snapshot_20261026_163501.jsonl.gz
./scripts/start.ps1 -NoBrowser
# 独立临时库的真实模型三级记忆与连续摘要验收。
backend/.venv/Scripts/python.exe scripts/evaluation/check_postgres_memory.py --with-compaction
```

当前仍为本机单 worker；数据库 advisory lock 拒绝第二个服务取得恢复执行权。SQLAlchemy、Saver 与 Store 各自管理连接及事务，线程 ID 不承担身份或授权。

P0 源码、工作区补丁、逐文件哈希和项目覆盖配置备份位于 `data/archives/postgresql_memory_migration/20261026_162109/`，其中本地配置不提交仓库。原 MySQL、旧 SQLite 和原始资源均保留。P0 源码已经包含电脑故障前的一部分 PostgreSQL 修改，不能把它直接当成已验收的 MySQL 服务回滚包。

恢复前先停止精确受管理进程并保全新 PostgreSQL 写入。资源重建可使用完整源快照和既定资源白名单向全空目标导入；不能向正在使用的库补写或直接切回旧库丢弃新记录。需要退回 MySQL 时，应使用对应已验收源码、原配置和备份，完成隔离验证后切换。
