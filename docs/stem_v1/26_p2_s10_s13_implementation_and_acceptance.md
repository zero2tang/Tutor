# 第三批：F1/F2 与 S10～S13 实现和验收

日期：2026-10-23。范围经用户确认补齐 F1/F2，再完成 S10～S13；允许修改现用实验数据库。**实现与本批验收已完成**。F3/F4、S14 及以后未实施。

## 1. 实际变化与统计口径

| 项目 | 第三批前 | 第三批后 | 变化 |
|---|---:|---:|---:|
| MySQL 实际表数 | 37 | 28 | −9 |
| 模型记忆相关表，不含独立 memory_items | 11 | 5 | −6 |
| 难度专用表 | 3 | 0 | −3 |
| context_checkpoints 记录 | 37 | 6 | 每会话／角色一个当前槽 |
| 生产源码物理行 | 30,280 | 29,593 | -687 |
| 生产文件实际删除 | — | 9 | 新增 8，生产文件总数净减 1 |
| 离线入口实际删除 | — | 1 | 旧难度构建 CLI |

生产增删为 **+2,047 / −2,734，净减 687 行**，对比第三批开始前 `source_before.zip`，范围为 `backend/app/**/*.py` 与 `frontend/src`。不把前两批未提交改动、提示词、测试、迁移脚本、文档、依赖锁计入该数。此次没有采用 git HEAD 作为第三批基线。提示词变化另见 acceptance.json；新增验收／归档脚本增加了离线代码量，不能宣称整个仓库都按上述数字减少。

数据库与文件数量的减少大于生产代码的缩减比例。框架适配仍需保留业务权限、预算、公开流与模型兼容逻辑，不能把所有保留代码说成冗余。

## 2. 实际删除文件

- `backend/app/agent/runtime/loop.py`
- `backend/app/agent/runtime/runner.py`
- `backend/app/agent/runtime/tool_batch.py`
- `backend/app/application/commands/courses/build_difficulty.py`
- `backend/app/domains/courses/difficulty/migrations.py`
- `backend/app/domains/courses/difficulty/models.py`
- `backend/app/domains/courses/difficulty/rubric.py`
- `backend/app/domains/memory/extractor.py`
- `backend/app/domains/memory/recording/legacy_barriers.py`
- `scripts/data/build_difficulty.py`

删除文件不等于删除全部职责。业务权限、租约、当前题、原答和评分提交继续保留；例如旧 runner 的生命周期职责迁入唯一的 application/agent/execution.py，原批工具安全校验迁入 tool_binding.py。下列入口是最终实现，不是旁路第二套执行器：

- `application/agent/runner.py` → `execution.py` → `graph.py`：唯一角色工厂。
- `agent/runtime/model_adapter.py`：现有 ModelClient 与标准消息／结构化输出的协议适配。
- `application/agent/tool_binding.py`：服务端可信范围与受控工具，不自行编排模型工具循环。
- `memory/recording/source_barriers.py`：旧撤销数据仍需运行时生效，删除的是旧模型／懒迁移机制。
- `courses/difficulty/metadata.py`：每题唯一估计与题面指纹失效检查。

## 3. F1：标准 Agent 底座

锁定 LangChain 1.4.7、LangGraph 1.2.16。`create_agent` 管理模型节点、结构化修复、工具路由和消息回填；ToolNode 负责工具并发。主助手、知识、练习与评估走同一工厂。无工具评分对上游只发 JSON mode，不开放业务工具。

当前 DeepSeek 部署的原生 JSON Schema 在 F0 被拒绝，因此显式使用 JSON mode＋ToolStrategy。WireChatModel 将最终 JSON 转为框架结构化响应；模型回传的真实工具调用仍通过注册表校验。自定义传输继续承担图片、Token 流、重试和模型出口限流，未强行换成 langchain-openai。

业务 ToolContext 由服务端注入；课程、学生、会话和题目范围不能由模型覆盖。故障计数、共同调用预算、超时、取消、LearningTurnLost 和私有评分隔离仍保留。LangGraph 只接管角色内执行；MySQL 仍是业务事实权威，未引入第二套长期 Store 或任意节点恢复承诺。运行时显式关闭 LangSmith tracing。

## 4. F2＝S11：语义压缩与薄缓存

SummarizationMiddleware 对完整来源消息执行摘要选择与替换；显式关闭默认 trim_tokens_to_summarize。摘要器按完整来源分块，最多两层，校验顶层短别名的完整覆盖。业务薄策略决定安全可压缩前缀、固定当前任务与硬请求预算；不是删除历史后只留最近 N 条。

context_checkpoints 只保留每会话／角色一个缓存槽，去掉 source_units 列。读取同时匹配来源指纹和工作区／记忆状态指纹；并发题只能覆盖缓存槽，不能读取另一题的摘要。保存冲突可放弃候选，正式消息和事实不回滚。原来的 37 份缓存已验证覆盖，留下最近 6 份；失效时从 messages 重建。

当前原题、原答、帮助状态、目标和未提交工具回执不进入可丢弃摘要。保留最终请求预算检查、无收益候选拒绝、有限摘要修复与服务商超限的一次安全重建。根据原计划 S11 的保护条款，`context/compactor.py`、来源分段、预算策略和临时缓存接口仍保留；没有为了删文件取消这些约束。F4 可以继续收敛接口，不能把本批说成已删除所有自定义上下文代码。现有 SSE 由框架执行入口映射回原公开协议，模型正文仍经 ModelClient 的 on_content 增量发布，未直接透传框架私有事件。

## 5. S10：十一表收敛五表

保留表：`memory_facts`、`memory_jobs`、`scope_memories`、`session_concept_observations`、`session_memory_extractions`。误概念提示 `memory_items` 独立保留。

- ScopeMemory 同行保存正文与 generation／invalidation_version／enabled／撤销来源／cutoff；不再维护单独状态表。
- SessionMemoryExtraction 同行保存水位、关闭版本、经历、偏好和提取结果；经历版本／覆盖水位与 v2 观察提取版本分开，旧经历不冒充新规则已处理。
- MemoryJob 通过 session_extract／scope_aggregate 区分两类任务；统一队列、恢复入口和租约，旧规则任务结清。租约 token 防止过期工作者覆盖新领取的任务。
- 固定知识点→课程→学生祖先失效替代通用依赖边；保留来源、反例、晋升门槛、纠正与按范围遗忘。粗粒度祖先失效可能增加必要重算，未宣称模型成本必然降低。
- 旧学生总结作为 historical_reference 保留正文与撤销屏障，不自动冒充新规则的有效总体记忆；不会在启动时遍历历史并触发模型重提取。

已迁移并核对 13 份旧学生总结、15 份经历、30 份会话水位、10 份 scope 状态。会话观察→知识点→课程→学生的模型递归归纳继续保留。正常新增不取消已冻结的运行快照；撤回、停用和来源失效继续阻止旧结果提交。

## 6. S12：有界目标树与共享评分提交

目标限制为最多 32 个节点、根下最多 4 层、返回栈最多 4 项。保留一个活动路径、父子关系、补救返回、稳定 goal_id 和专用版本。任意跨分支依赖图改成 prerequisites；新前置条件只能引用已完成目标，旧前置条件不能直接删除或标为完成。本库 23 份目标没有实际依赖边，但迁移仍逐节点比较 ID、状态、证据、活动节点和返回题指纹。

单题与整组共用 AssessmentService.commit_attempt，在调用方事务内写 Attempt、证据关联、逐知识点状态与误概念。整组仍使用一次整体模型评估和整体事务，不逐题调用会独立提交的单题 API。并发生成、独立求解复核、同组去重、全组通过后发布、提交哈希与幂等回执均保留。

## 7. S13：单一题目难度

每课程保留一个 ready 口径：数学 `math:difficulty:e830dc9a76c8385ae0a8`，力学 `mechanics:difficulty:0f8a66e0d4def9e634c8`。7,829 条题目分数迁入 Question.difficulty_estimate，其中 7,778 条 estimated、51 条 unknown；原始来源哈希全部匹配后才迁移。保留 method、version、reference_group、量纲／方向、source_hash、模型与置信度。

删除多 profile 比较、标注次数加权的概念难度和专用构建链。`/api/difficulty/profiles` 返回 410；知识点数值排名退出工具与回退逻辑。题干、答案、解析、关联知识点、版本或图片变化使旧数值失效；未知不填 0，公开接口不返回答案／解析／私有推理明细。

当前难度查询在课程内取候选、验证完整题面指纹后由 Python 筛选排序，适合当前约八千题的数据量。没有性能压测，不承诺大题库吞吐；后续如需要应设计索引，而不是恢复原多表体系。

## 8. 数据库退役与可恢复性

| 已删除表 | 归档记录数 | 去向 |
|---|---:|---|
| student_memories | 13 | scope_memories 的历史正文与 legacy_barrier |
| session_experiences | 15 | session_memory_extractions 的经历字段 |
| memory_session_states | 30 | session_memory_extractions 的水位与关闭状态 |
| memory_scope_states | 10 | scope_memories 的状态及撤销字段 |
| memory_dependencies | 0 | 固定三级祖先失效 |
| memory_scope_jobs | 0 | memory_jobs 的 scope_aggregate |
| difficulty_profiles | 4 | 保留口径元数据；其余历史归档 |
| difficulty_scores | 10,615 | 选定 7,829 题分数迁入题目，其余归档 |
| question_concept_weights | 9,329 | 历史归档，退出加权算法 |

归档共 20,016 行旧数据。完整 37 表 DDL＋类型化数据快照和第三批前源码保存在 `data/archives/simplification_p2/20261023/`（忽略提交），迁移脚本为 `scripts/maintenance/simplify_p2.py`。9 张退役表均通过临时表完整回灌与逐表哈希比对；字段映射通过后才删除旧表。迁移曾遇到 DECIMAL→JSON 类型转换及自动更新时间变化，均已修正并复验，未用放宽比对掩盖差异。

退表前 22 张未改业务表哈希一致；正常初始化连续运行两次仍是 28 表，没有复活旧表。之后的检索回放只刷新既有 index_tasks 的 token／updated_at，其他原业务记录哈希仍一致。没有启动历史记忆任务。

回退需停止新请求并核对已提交回执，再结合源码快照与数据库归档恢复到明确业务边界；不能只恢复一张旧表或任意复活旧执行循环。当前不提供跨底座任意模型步骤接续。

## 9. 验收证据与限制

- 真实 MySQL＋进程内路由验收：`data/reports/simplification_p2/acceptance.json`；28 表、重复启动、原记录、教材三类提取读取、公开难度与旧 profile 退役均通过。
- 数据回灌／映射：`migration_verification.json`；9 表全部恢复通过、7,829 条分数、23 份目标逐项比较通过。
- 最终后端全量：**774 项通过**，耗时 520.12 秒；全量后最后的队列恢复收敛以 **66 项专项通过**覆盖。初次最终全量曾为 773 通过／1 失败，公开缓存 Token 字段白名单修复后已全量复验。测试 XML 与汇总见 backend_final.xml、queue_final.xml、validation.json。
- 前端原有流式／恢复 15 项通过，TypeScript＋Vite 构建通过；没有新增事件持久表。
- 本地检索：`retrieval_replay.json`，精确题号与图文三通道保留，16 题回放上游请求 0、新投影 0。知识点 GraphRAG 关系未改；没有把这次检索回放说成新的全库 GraphRAG 质量评估。
- 当前 deepseek-flash 隔离真实验收：`real_generation.json`。仅自编素材和新 SQLite，不读取实验 MySQL；三阶段出题通过，父→知识子→父接续通过，来源完整语义摘要通过。9 次模型调用、39,381 输入 Tokens、2,639 输出 Tokens，模型调用耗时合计约 19.9 秒。自编重复历史的输入估算从 26,546 降至 581 Tokens，仅是该示例，不能外推真实学习效果或通用压缩率。
- 新增依赖的生产闭包锁定在 requirements.lock.txt；pip check 通过。F0 的可选 langchain-openai/OpenAI SDK 不成为生产前提。

旧 v1 记忆自由文本测试和已退役的难度加权／多 profile 测试改为合并字段、唯一来源、指纹失效、权限与租约测试；总测试数量发生变化。不能把新测试数与前两批 794 项直接当成覆盖率提升或等量保留。本批保留的核心业务测试包括递归归纳／遗忘、GraphRAG 边界、目标返回、并发生成与整组提交、长对话来源覆盖及 SSE 取消／恢复。

本批没有替换 Neo4j、取消 GraphRAG、削去并发组、把长对话改成截断，也没有进入 F3/F4 或 S14 以后。下一批应继续按原计划独立验收，而不是把本批新增框架适配视作全部简化已经结束。
