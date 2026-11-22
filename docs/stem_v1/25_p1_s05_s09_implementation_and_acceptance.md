# S05～S09 实施与验收记录

日期：2026-10-23。第二批已实施；六项核心能力保留。生产 Agent 仍使用原底座，F1 及 S10 以后没有开始。

## 1. 实际结果与统计口径

实验数据库 **44 → 37 表**：物理删除 8 张旧表、新建 1 张 `model_calls`。累计两批由 49 表减至 37 表。教材、题目、来源 ID 保留；核心图关系、记忆、主子目标、冻结练习、正式原答与压缩检查点的表内容哈希与本批基线一致。

本批生产源码（`backend/app/**/*.py` 与 `frontend/src`）**+347 / −337，净增 10 行**，不能说代码量已经下降。主要收益是实体和状态收敛、少一次呈题前预测、停止每次检索写 SQL 回执。新增的参考版本核验、租约边界、受控诊断字段及日志降级抵消了部分删除量。计数按本批修改前文件快照比较，不用混有此前重构的 Git HEAD 冒充本批基线；逐文件结果见 [acceptance.json](../../data/reports/simplification_p1/acceptance.json)。

完整删除的生产文件只有两份：

| 文件 | 删除内容 |
| --- | --- |
| [原 practice/models.py](../../backend/app/domains/practice/) | `ExerciseGenerationAttempt` 独立 ORM 模型，诊断收回生成运行 |
| [原 assessment/predict.md](../../backend/app/agent/skills/assessment/) | 呈题前预测 Skill |

`ExtractionJob`、`ExtractedBook`、`TeachingPack` 等旧类在仍有其他职责的文件内移除，不能计作整文件删除。提示词文件不在 Python/前端行数口径中。另新增迁移及专用验收脚本 435 行（254+97+84），以及测试、文档；这些是额外工程代码，没有混作运行代码减少。

## 2. 逐项落地

| 步骤 | 当前实现 | 实际退役表（归档行数） |
| --- | --- | --- |
| S05 | PDF 原件按旧 book ID 并入 documents；8 份旧解析快照保留，共 9 份。任务字段进入对应小节版本，提取状态与复核状态分别记录。旧 jobs API 从小节投影返回。 | `textbook_extraction_books`（1）、`textbook_extraction_jobs`（15） |
| S06 | 52 份每题附属教学参考迁入 `Question.teaching_reference`；新参考不重复保存完整题干、答案及原解文本。旧包完整载荷归档，legacy hash 与 reference hash 分开保存；旧公开目录仍投影读取，前端以 question_id 开始讲题。 | `teaching_analysis_packs`（52） |
| S07 | 教材分页与题图调用进入 `model_calls`，保留 212 个旧调用 ID、响应、失败、模型及用量信息。按 purpose+request_hash 共享成功缓存查询，失败不命中，允许多次尝试。 | `textbook_extraction_calls`（137）、`exercise_search_calls`（75） |
| S08 | 构建租约进入 `index_tasks`，身份包含 course+namespace+target；保留原子 claim/renew、token 过期与发布前检查。检索返回 receipt_id/trace_id，诊断写有界 JSONL，写日志失败不影响检索。 | `exercise_search_build_leases`（1）、`exercise_search_receipts`（216） |
| S09 | 删除呈题前预测调用、Schema、反馈关联与提示词；呈题仍冻结契约、目标和帮助状态。保留生成→盲解→语义复核，诊断存生成 `AgentRun.private_diagnostics`，关联候选、次数和三个运行。 | `exercise_generation_attempts`（54） |

八张退役表共 **651 条记录**全部归档，并以 MySQL 临时表逐表还原、比对内容哈希后才删除。连续两次正常初始化仍为 37 表，旧 ORM 与时间精度迁移入口不会重新建回旧表。

主要代码入口：

- [ExtractionStore](../../backend/app/domains/materials/extraction/store.py)、[小节模型](../../backend/app/domains/materials/extraction/models.py)、[教材原件模型](../../backend/app/domains/materials/models.py)。
- [教学参考及旧目录投影](../../backend/app/domains/materials/teaching_reference.py)、[教学目录读取](../../backend/app/domains/materials/teaching_packs.py)、[来源访问检查](../../backend/app/domains/materials/resources/access.py)。
- [统一模型调用](../../backend/app/infrastructure/persistence/model_calls.py)、[检索存储](../../backend/app/domains/courses/questions/search/store.py)、[有界回执日志](../../backend/app/infrastructure/persistence/retrieval_log.py)。
- [三阶段生成复核与私有诊断](../../backend/app/domains/practice/reviews.py)、[历史诊断寻址](../../backend/app/domains/practice/diagnostics.py)、[运行公开字段](../../backend/app/domains/sessions/service.py)。

旧 pack payload/catalog hash 不从 Question.answer 反算，52 个历史引用仍按旧完整载荷核对。归档文件是这些历史版本继续读取的依赖，部署或备份应一并携带，不能只复制 MySQL。旧生成 audit_id 可由服务端诊断读取器定位档案，新 audit_id 为生成 run_id。`/runs` 不返回私有诊断；习题公开摘要、错误和工具轨迹也收紧，防止旧列间接泄露答案。资产、审核表示、发布投影三表继续保留。

检索日志默认位于 `data/logs/retrieval.jsonl`，每份 8 MiB、最多保留 2 份轮转备份；这是按容量淘汰，不提供 SQL 历史回执查询。在线读图和重排即使 cache=False 仍写 model_calls，因此不能说检索完全没有 SQL 审计写入。

## 3. 验收证据

- **全量后端：794 passed**，172.93 秒；最后收敛共享缓存和参考读取后，**受影响业务 100 passed**。分别见 [完整回归](../../data/reports/simplification_p1/backend_final.xml)、[最终专项回归](../../data/reports/simplification_p1/backend_affected_final.xml)。新增 4 项重点验证跨目的缓存、过期租约、日志故障与私有答案边界。
- **前端：15 passed**，生产构建通过。见 [流式与恢复](../../data/reports/simplification_p1/frontend_tests.log)、[构建](../../data/reports/simplification_p1/frontend_build.log)。
- **实际 MySQL 与路由**：9 份原件/快照、15 小节、52 参考、212 调用保留；7,973 道题的公开来源指纹不变。52 个旧包 ID/hash 均可公开读取而不返回原解，内部教学参考与旧载荷一致；核心业务表哈希未变。1.1 仍含 5 个定义、15 道习题、2 个方法，completed 与 reviewed 区分正确。见 [迁移与还原](../../data/reports/simplification_p1/migration_verification.json)、[路由及来源](../../data/reports/simplification_p1/acceptance.json)。
- **原页档案**：重新运行第一章原页来源/图片资产/快照检查，既有 1.1 逐页复核回执与当前哈希一致。见 [第一章验收](../../data/reports/textbook_extraction/chapter-1-acceptance.json)。本批没有重新提取课文，没有扩大章节范围，也不把此前其他小节的模型审校当成人工逐字复核。
- **真实本地 ES、MySQL、已有模型缓存**：精确检索、旧查询接口、课程范围拦截、外来资产拦截、含图三通道查询通过；16 题索引重放 **0 新上游请求、0 新投影记录**，索引 generation 不变。见 [检索与缓存重放](../../data/reports/simplification_p1/retrieval_api_and_replay.json)。外部出口在该脚本中被硬性禁止，不能据此宣称新请求模型链路重测成功。
- **真实生成三阶段**：另用只含脚本自编计数素材的新建 SQLite 空库运行实际 PracticeService/AgentRunner，完全不读取实验 MySQL。当前 `deepseek-flash` 完成 3 次请求，三个 run 均可定位、候选被接受、私有诊断保存、预测运行数 0。成功复跑耗时 5.64 秒，输入 5,092 / 输出 717 token。见 [真实生成报告](../../data/reports/simplification_p1/real_generation.json)。该结果验证一个自编样例的工程链路，不代表生成正确率；不是当前 MySQL 中新增了一道题。

首次全量运行遇到默认 pytest 临时目录权限冲突；后改为项目内隔离临时目录。中间发现并修正旧模型导入、参考模块循环依赖、兼容投影读取与旧预测子运行断言，失败记录未覆盖。真实生成首轮业务执行已完成，但验收脚本误从冻结契约读取内部 validation 字段，改为从 Question 读取后复跑成功；上述时间/token 仅计成功复跑，不冒充本轮全部模型费用。

原含图验收曾因可能向外部服务发送现有数据库内容被自动审批拒绝。最后改为本地服务与已有缓存的硬性出口限制；真实生成则单独用自编素材空库通过审批，没有绕过原外发限制。

## 4. 六项能力与后续边界

递归记忆、知识点 GraphRAG、会话主子目标、并发习题组与原子提交、语义压缩和 SSE 均保留，原有专项回归继续通过；真实原答、掌握状态和冻结契约没有被迁移诊断替代。没有重做真实学生长期学习实验或学校级并发压测。

原件与任务合表不代表删除多种解析策略；模型调用合表不代表统一提示词工作流；取消预测不影响基于真实原答评分。当前仍使用自定义 Agent 运行器；下一窗口按统一计划开展 F1，不在本批顺带实施 S10 以后。

本地完整快照与来源备份位于 `data/archives/simplification_p1/20261023/`（已忽略，包含私有档案，不提交仓库）。迁移工具：[simplify_p1.py](../../scripts/maintenance/simplify_p1.py)。综合证据：[verification.json](../../data/reports/simplification_p1/verification.json)。
