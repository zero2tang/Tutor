# 第四批：F3/F4、S15/S16 实现与验收

日期：2026-10-23。**本批已完成验收，S14 暂不实施。** 保留三级递归记忆、知识点 GraphRAG、会话主子目标、并发习题生成与整组提交、语义压缩、基础 SSE 六项能力。本记录以第四批开始前的独立快照为基线，不与前三批的未提交改动混算。

## 1. 实际结果

| 指标 | 第四批前 | 第四批后 | 变化 |
|---|---:|---:|---:|
| MySQL 实际表数 | 28 | 28 | 0 |
| 生产源码物理行 | 29,593 | 29,339 | -254 |
| 生产文件 | — | 删除 5、新增 2 | 净少 3 |
| 旧离线验收入口 | — | 删除 1 | 资料转交延迟验收退役 |
| 20 个讲题工作区 JSON 字节 | 567,652 | 83,786 | −483,866 |
| 会话上下文＋消息元数据 JSON 字节 | 1,685,743 | 1,426,134 | −259,609 |

生产源码 **+533 / −787，净减少 254 行**，统计 `backend/app/**/*.py` 与 `frontend/src`，包含空行和注释。新建的恢复图／历史适配文件合计 **367 行**，包含在新增数内。测试源码净减少 214 行；离线归档／验收代码净增加 224 行，文档和配置另计。不能把生产源码的减少说成整个仓库都减少了同样数量。

工作区原来重复保存的内容迁入唯一消息及其有类型附件；消息 metadata 因承接回执而增加，所以上表也列出两列 JSON 的合计。字节为固定 JSON 序列化的 UTF-8 逻辑载荷，不是 InnoDB 磁盘占用；没有宣称物理数据库文件自动缩小。

## 2. 实际完整删除文件

- `backend/app/agent/context/storage.py`
- `backend/app/agent/runtime/payloads.py`
- `backend/app/application/learning/continuation.py`
- `backend/app/application/learning/materials.py`
- `backend/app/domains/materials/transfer.py`
- `scripts/evaluation/check_latency_optimization.py`

删除旧文件后，没有保留自动切回旧 Agent 的旁路。旧 `continuation.py` 的接续职责由小型图接管；旧 `payloads.py` 中唯一仍必需的工具文本投影进入现有 `application/agent/registry.py`，未重新建立一个同名空壳。旧延迟脚本断言父子资料转交必须发生，随该协议退役；其源码及历史报告保留在归档和原报告目录。

## 3. F3：五节点作答接续图

入口为 `LearningService.answer()`／`answer_batch()` → `application/learning/answer_graph.py`，统一走：

```text
prepare_assessment → commit_assessment → publish_feedback
    → continue_learning → publish_continuation
```

这是五个实际图节点，各自调用领域准备／事务函数，并非把整个 answer 包成单节点。单题与整组的 `prepare` 只生成临时诊断；`commit` 在短事务内提交正式证据。提交节点结束后清除图中的临时 prepared 数据。整组仍一次模型评估、一次整体事务，生成仍保留最多 3 个并发任务及全组通过后发布。

`Attempt`／`BatchSubmission` 是评分权威回执，正式评估消息和带 `continuation_for` 的成功主助手消息是发布回执。重试根据这些记录跳过已完成部分：

- 评分提交后丢响应：读回已提交作答，不再次评分或增加证据。
- 反馈尚未写入：补写反馈；反馈已保存但 SSE 断线：重发同一个消息 ID，不新增记录。
- 下一步模型失败：评估反馈仍可见，错误通知不被当成完成回执，允许同次提交补齐后续安排。
- 主助手正式消息已保存后断线：读成功消息中的 plan，后续重试不重新出题或运行主模型。
- 进程内检查点丢失：新建图，从正式业务边界重建；不声称可以恢复任意模型内部步骤。
- 执行权丢失：每个节点检查 active_turn_id，旧任务不能覆盖新持有者。

使用请求内 InMemorySaver，不新增检查点表或长期内存 Store；`thread_id` 只关联图运行，不承担身份和执行权校验。纯评分 API 仍可使用领域 `assess` 包装器，网页作答接续只有上述一个图入口。没有预测／反思研究子图。

## 4. S15：退出资料引用池与只读执行位置恢复

`mat_*` 引用池、父子资料选择／继承链、`selected_material_ids`、转交回执与 ToolContext 的资料授权字段已退出运行协议。旧参数会被 Schema 拒绝。子助手自行使用可信课程／会话范围中的只读工具；当前题仍由服务提供公开快照。旧的内部答案与题目授权边界继续保留，不能靠 task 文本扩大范围。

不再从失败检查点恢复只读工具故障次数，也不持久化这类计数。删除了故障策略的初始计数与持久化回调接口。当前运行内仍保留必要的 3 次同名工具故障熔断和并发保护；它们没有承担跨失败执行位置恢复。

`delegation_checkpoint` 保留请求 ID、已占用的子任务额度、完成的业务结果，原因是“题目已发布、子回执尚未保存”的窗口仍需按正式业务记录恢复。这是业务幂等回执，不能因使用 LangGraph 就删除。恢复成功的练习组、冻结题和讲解回执不会重复执行。

同一 request_id 的新运行登记按 `agent_runs.private_diagnostics.request_id` 关联，评分与接续使用同一个稳定派生 ID，锁会话行后检查最多 64 次角色运行，不新增费用表。每个运行仍有工具／轮数与有限修复上限，委派额度仍在执行前保存。该限制计入失败运行，并跨新的 Runner 对象生效；它不是金钱预算或 Token 精确计费，也不把后台记忆任务纳入同一请求账本。

退出资料转交可能带来重复只读检索。本批记录实际模型调用，未将该取舍表述为必然减少 Token 或降低成本。

## 5. S16：讲题历史与私有参考解按消息存储

`guided_teaching` 只保留当前题、待答问题、帮助状态、版本、来源和关联消息 ID 等当前状态，不再保存整份 dialogue／support_history／evidence／prior_observations，私有 analysis 也通过消息附件读取。

- `Message.content` 保留原始学生输入和已验证的公开正文。
- `_guidance_observation` 保存模型观察、原答消息 ID 与提交前帮助条件；不重复复制 raw_input。
- `_guidance_support` 保存 problem_id、help_level、exposure_types、detection_method 等类型化帮助事实，公开帮助片段用字符区间关联对应消息。无法定位为同一公开片段的旧记录保留明确标记的原摘录，不凭正文重猜归属。
- `_guidance_analysis` 私有附件保存参考解；换题后仍按原消息可审计。该字段不会出现在公开消息，也显式排除在长期记忆 MessageFact 之外。
- `history_refs`、少量前题消息 ID 按需重建模型需要的历史；基础语义摘要继续处理超长上下文，不把引用式存储当作截断历史。

新题讲解、普通学习两个入口保留。讲题正文仍是验证后分段发布，不直接透传原始模型草稿；普通主助手正文仍即时流式输出。

## 6. F4：实际删除与保留边界

唯一角色底座仍是 `application/agent/runner.py` → `execution.py` → `graph.py`／create_agent，唯一作答接续是五节点图。调用关联沿框架 RunnableConfig 传播，移除了独立的 `_current_invocation` ContextVar；必要的可信运行元数据薄适配仍在 invocations.py。

取消未参与压缩决策的 context_target_ratio／target_tokens 展示配置、空 started_details 转发，以及无持久化空缓存对象。按来源、工作区和撤回指纹验证的当前摘要缓存仍保留。

以下文件仍有实际职责，**没有声称已经删除**：contracts.py 的当前角色准备结果与类型端口，tool_policy.py 的本次运行内故障边界，invocations.py 的可信调用关联，context/compactor.py、policy.py、summarizer.py 的来源完整性／固定任务／硬预算保护。它们不是备用旧执行器。保留这些规则是原计划的安全条件，不通过删掉必要保护来追求更大的行数降幅。

直接与传递依赖未增加，LangChain 1.4.7／LangGraph 1.2.16 的生产锁文件未变；Neo4j、Elasticsearch 和既有向量投影继续保留。S14 没有实施。

## 7. 数据库与迁移验收

没有 CREATE／DROP 表，没有增删业务列；28 张表的 SHOW CREATE TABLE 与基线完全一致。

20 个工作区的 92 份观察、65 份帮助记录及 20 份私有参考解完成迁移并逐项重建验证。实际更新 28 个会话的 JSON、112 条消息的 metadata；原消息 ID、正文、创建时间、正式作答、能力证据和业务版本未改。当前库原先没有 `_previous_guidance_workspace` 完整快照，迁移并未虚报删除这类历史记录；代码已停止继续生成它们。

除 learning_sessions.pending_context 和 messages.metadata_json 之外，其余 26 张表的整表哈希一致，包括 attempts、batch_submissions、evidence_concept_links、student_states、memory_facts、三级记忆、session_goal_states、concept_relations、教材和检索投影。正常初始化连续两次仍为 28 表，未触发历史记忆补扫。

归档：`data/archives/simplification_p3/20261023/`，包括全部 28 表 DDL／类型化数据及本批前源码。初始归档过滤遗漏的 styles.css 已按基线 SHA-256 验证字节完全一致后补齐，未拿修改后的文件冒充基线。迁移脚本为 `scripts/maintenance/simplify_p3.py`；私有解附件调整曾触发 ORM 更新时间，已恢复原时间并再次全量核验，不放宽比对。

回退需停止新写入并核对已提交回执，再配套恢复本批源码与数据库快照；不能只把新代码换回旧代码，或只恢复工作区 JSON。迁移／验收脚本是离线运维工具，不是线上启动前提。

## 8. 验收证据

- 后端全量：**767 项通过**，约 533.89 秒；最后清理后的专项 **118 项通过**。全量后的故障恢复／回调构造接口清理由 118 项专项覆盖；最后将评分与接续费用 ID 统一，经 **54 项专项通过**。XML 与 validation.json 保存具体结果。
- 旧资料池测试 20 项改为 4 项协议拒绝／边界测试，新增 8 项作答图故障与费用测试、1 项引用存储测试；总数从 774 变为 767，不把数量变化冒充覆盖率提高。
- 前端流式／恢复 **15 项通过**，TypeScript＋Vite 构建通过。
- 真实 MySQL＋路由：教材 1.1 节仍可读到 5 类定义记录、15 道习题、2 条方法记录及教学模式，公开结果不含题解。
- 真实 Neo4j：健康查询及 2 跳扩展通过，本次样例 9 个节点／10 条边；SQL 关系与图客户端代码未变。这是运行链验收，不是 GraphRAG 全库质量评测。
- 当前 deepseek-flash：隔离 SQLite、自编计数素材与合成偏好。三阶段出题、父→子→父返回、评分提交后丢响应再接续、讲题引用重建、总体偏好归纳、语义摘要全部通过。同一提交的最终再次回放新增模型请求 **0**。
- 最终真实链共 **20 次模型调用**，输入 **78,655 Tokens**、输出 **6,041 Tokens**，模型调用耗时合计约 **51.31 秒**；详见 real_model.json。脚本未读取实验 MySQL／真实教材内容并发送给外部模型。模拟作答不是学习效果证据，也不据此承诺通用准确率、成本下降或并发容量。

完整机器记录：`data/reports/simplification_p3/acceptance.json`、migration_verification.json、real_model.json、backend_final.xml、final_followup.xml、billing_final.xml、source_after.json。
