# N0／N1 原生模型底座实施与验收

日期：2026-10-23。范围：[28 号计划](28_langchain_langgraph_native_architecture_plan.md)的 N0、N1；N2～N6 未实施。六项核心能力与现有业务数据保留。

## 1. 本批交付与三条约束

本批已交付：基线归档、评分事务执行权修复、v1 状态／事件／回执契约冻结、SQLite saver 隔离兼容验证、标准 ChatOpenAI 工厂，以及纯评分／独立求解／复核的直接结构化模型调用。

三条约束已加入 28 号计划第 19.1 节，并按本批范围执行：

| 约束 | 本批落实 | 剩余边界 |
|---|---|---|
| 替换一条路径，就退出对应旧路径 | `use_tools=False` 的业务输出直接进入纯结构化函数；不再为该输出创建 Agent 或 WireChatModel，失败也不回退旧 chat | 自主工具 Agent 仍依赖旧桥接，N2 退出；若上下文保护触发摘要，该摘要仍是单独的旧接口消费者，N5 退出 |
| 仅将有分支／并发／恢复意义的步骤上图 | 纯评分和复核直接调用模型，领域函数保持原职责；未新增生产图节点、服务层或通用执行器 | 生产会话图、Send 与 interrupt 均留在 N3／N4 |
| 分开核算替换与新增能力成本 | 生产代码、测试、一次验收脚本、文档和依赖分别统计；明确本批净增及未完成的删除项 | N1 尚未达到底座净减目标，不能把标准模型接入描述成已经完成整体精简 |

## 2. N0：基线与事务正确性

### 2.1 可恢复基线

归档在 `data/archives/native_n0_n1/20261023/`，该目录由 Git 忽略：

- `snapshot.json.gz`：28 张业务表的完整类型化数据和 DDL，保存原有题目、消息、原答、记忆、来源及资产引用。
- `source_before.zip`：本批生产源码、测试、文档和依赖清单的工作区归档；以实际未提交内容为准。
- [before.json](../../data/reports/native_n0_n1/before.json)：源码与各表的数量／哈希、归档校验值。

本批没有 DDL、裁表、启动数据迁移或正式学习库写入。[只读核验](../../data/reports/native_n0_n1/baseline_verification.json)检查全部表的 DDL 与数据哈希一致；业务表仍为 28 张。生产 API 及业务 Schema 的 30 份源码与归档一致，OpenAPI 单独保存到 [openapi.json](../../data/reports/native_n0_n1/openapi.json)。

### 2.2 评分执行权问题

原[评分事务](../../backend/app/domains/assessment/service.py)允许 owner 为 None。新增故障注入精确覆盖：外层图节点完成 owner 检查之后、评分事务开始之前，执行权被清空或转交。

修复前 `transaction-None` 用例失败：虽然下一节点最终报执行权丢失，待答题已经被清除、评分已经提交。保留[修复前失败证据](../../data/reports/native_n0_n1/n0_before_fix.xml)。修复后事务要求非空 turn 严格匹配当前 owner，撤销时不新增 Attempt／逐点证据，待答题保持；同一提交键的事务内重复检查也核对完整原答。

原有直接调用评分服务的正向测试补齐显式 owner；没有放宽生产校验。原答、账本、学生状态及记忆事实的同事务回滚测试继续保留。

### 2.3 协议冻结与 SQLite 兼容

[v1 Schema](assets/native_protocol_v1.schema.json)冻结 message、answer、batch_answer、followup、clarify、change_goal、skip、close 八类事件、执行 State 与接收／完成回执。其内部引用已校验；接收回执不等于完成回执，完成必须引用正式结果。它是迁移契约，尚未接入 HTTP 路由或生产图。

锁定并验证：LangChain 1.4.7、LangGraph 1.2.16、langchain-openai 1.7.2、langgraph-checkpoint-sqlite 3.1.2。此次新增安装仅 SQLite saver、aiosqlite 和 sqlite-vec；已有框架没有升级。生产依赖显式声明 provider 和 saver，其传递依赖补入锁文件。

N0／N1 的 SQLite 探针使用两个独立 Python 进程，验证相同 thread 的 interrupt→退出进程→重新编译图→Command 恢复，准备节点只执行一次；同一 saver／图下三个线程互不串状态，删除一个线程不影响其他线程。一次性脚本现已退役，源码保存在[阶段源码档案](../../data/archives/native_remaining/20261023/ancillary_reference.zip)的 `scripts/evaluation/check_native_checkpoint.py`，当前生产恢复验证见[30 号记录](30_native_remaining_implementation_and_acceptance.md)。

[兼容报告](../../data/reports/native_n0_n1/checkpoint_compatibility.json)中隔离文件为 32,768 字节，含 `checkpoints`、`writes` 两表。生产未新增此存储。已验证 `adelete_thread` 整线程清理；未采用按单个 checkpoint 裁剪历史的非公开机制，N3 仍需明确生产保留策略。

## 3. N1：已切换的真实调用链

```text
领域服务：评分／独立求解／复核
  → 既有 AgentRunner：准备输入、运行记录、超时和取消
  → run_agent_loop 的无业务工具入口
  → run_structured_model：上下文保护、有限修复、公开字段投影
  → invoke_structured：完整请求预算、共享三并发
  → 标准 ChatOpenAI.with_structured_output
  → 原始工具参数严格校验 → 既有领域校验／事务
```

新增文件职责：

- [infrastructure/clients/chat.py](../../backend/app/infrastructure/clients/chat.py)：标准模型工厂和纯结构化请求。与旧聊天／embedding 共用 ModelClient 的异步连接和并发额度；SDK 客户端复用，单次请求选项用浅复制隔离，应用退出时统一关闭。
- [application/agent/structured.py](../../backend/app/application/agent/structured.py)：纯业务模型调用的薄生命周期；不创建通用 Agent，不执行业务工具，不新增任务或操作表。

生产调用显式使用 `method="function_calling"`、`include_raw=True`；从真实 function arguments 以 `strict=True` 再校验，拒绝数值字符串／布尔值误当整数及额外字段。SDK 的原始 JSON 文本不会被人工补造成工具调用。

完整请求守卫计入系统提示、工具 Schema、图片和输出预留。SDK `max_retries=0`；结构／领域校验最多三次，网络失败、截断或取消不作为结构修复重放。供应商上下文超限仅允许既有控制器实质缩小请求后重建一次。

结构化函数参数的公开正文由原生 callback 接收，再经既有顶层字段白名单解析；业务工具参数、私有字段和推理不对外。用量缺失／失败标记为 `partial_or_unknown`，整数计数只表示已报告量的下界，不等于服务商账单。

`create_chat_model` 本身只提供标准模型；N1 正式纯调用的限流在 `invoke_structured`。N2 将标准 Agent 接入生产时，仍须在其中间件接入同一限流和根预算，不能直接裸调用工厂后宣称已受所有约束保护。

## 4. 保留消费者与退出阶段

| 当前消费者 | 本批状态 | 退出／迁移阶段 |
|---|---|---|
| 独立评分、整组评分、独立求解、语义复核等 `use_tools=False` | 已切标准 function calling；原纯调用 Agent 路径不可再达 | 本批完成；无旧路失败回退 |
| 自主主助手、知识、习题工具角色 | 仍使用 WireChatModel＋BusinessTools 及原工具调度 | N2；同时退出占位工具、重复 Schema 和第二消息推进 |
| 历史 SemanticCompactor／手工摘要钩子 | 来源覆盖、撤回、完整任务保护保留；仍有旧模型适配消费者 | N2 保护先接入，N5 收敛并退出旧控制器 |
| 两类后台记忆任务中的直接 chat | 保持原服务与存储 | N4 迁移必要的模型调用，不能新增第二套记忆队列 |
| 教材提取、含图检索／重排、embedding | 保持现有独立接口 | 不借 N1 重写；有真实消费者时不能整文件删除 llm.py |

因此本批没有完整删除生产文件。`WireChatModel` 不能仅因纯评分迁出就整文件删除；其全部消费者退出才满足 28 号计划第 15 节的条件。[消费者静态清单](../../data/reports/native_n0_n1/baseline_verification.json)列出剩余 `.chat`／`.embed` 调用位置，不将静态扫描当作调用量统计。

## 5. 验证结果与证据边界

| 检查 | 结果与证据 |
|---|---|
| N0 作答恢复、事务和整组回归 | [44 项通过](../../data/reports/native_n0_n1/n0_regression.xml) |
| 标准 provider、连接生命周期、旧流式及用量 | [63 项通过](../../data/reports/native_n0_n1/provider_contracts.xml)，含缺失用量不能补零、新旧混合请求真实达到三并发、三次失败终止且不回退 |
| 后端完整回归 | **792 项通过，155.36 秒**，见[最终 XML](../../data/reports/native_n0_n1/backend_regression.xml)、[日志](../../data/reports/native_n0_n1/backend_regression.log)及[总验收报告](../../data/reports/native_n0_n1/acceptance.json)；各专项与全量存在重叠，不相加 |
| 前端流式／恢复 | 15 项通过，TypeScript 与 Vite 构建通过 |
| 依赖 | pip check 通过；锁文件离线 dry-run 无额外安装需求，见[依赖解析日志](../../data/reports/native_n0_n1/locked_dependencies.log) |
| 正式库与接口 | 28 表 DDL／数据哈希不变，30 份 API／Schema 源码不变 |

N1 真实模型脚本的[最终报告](../../data/reports/native_n0_n1/real_model.json)为 7／7；脚本已退役，历史源码保存在[阶段源码档案](../../data/archives/native_remaining/20261023/ancillary_reference.zip)的 `scripts/evaluation/check_native_models.py`：

1. 标准结构化模型读取原页图片，得到体重变化 `2、-1、0`。
2. `create_agent + ToolStrategy` 真实执行课程目录工具，再返回嵌套来源契约；只是隔离兼容探针，不表示生产工具桥接已退出。
3. 独立求解走生产 Runner 的标准出口，主动拒绝第一次候选后第二次通过。
4. 真实调用嵌套 SolutionReview，并通过现有语义复核规则。
5. 独立评分提交正式原答和一个 Attempt；同键重放零新增模型请求；公开反馈有实际增量。
6. 三个标准请求并发完成，共享槽位释放。
7. HTTP 分发钩子之后取消，确认任务取消及槽位释放；该次服务商用量未知。

本批只用原约定页图与自编计数素材，业务链写入隔离 SQLite。首次脚本误用了不存在的 `validate` 技能，失败发生在模型发送之前；保留 `real_model_previous_*.json`，修正为生产采用的 `understanding` 后重验。没有使用真实学生档案，不证明教学效果、题库检索质量、多 worker 或生产会话持久恢复。

## 6. 成本与精简结果

本批生产 Python 从 **235 文件／22,899 行**变为 **237 文件／23,130 行**，净增 **231 行**。runtime／context／application-agent 三组非初始化源码由 3,342 变为 3,440 行，净增 98 行。统计包含空行与中文注释。

这 231 行属于标准模型替换和必要的用量／连接处理；本批没有新增生产会话图、持久工作流或业务表。测试、三个一次验收脚本、协议 Schema 和文档分别列在[源码成本报告](../../data/reports/native_n0_n1/baseline_verification.json)，不混入生产代码。

该结果应记为原生迁移的阶段成本，**不记作代码精简成功**。旧纯模型调用路径已经不可达，但旧工具消费者仍在，尚不能退出整份适配文件。N2／N5 必须按清单删除对应旧路径；若最终只有这些净增而无实际退役，整体简化目标仍未达到。

## 7. 复现与下一阶段

以下常规命令仍可从项目根目录执行；结果对应执行时源码，不代表重新验收 N1 快照：

```powershell
backend/.venv/Scripts/python.exe -m pytest backend/tests -q
backend/.venv/Scripts/python.exe -m pip check
```

`check_native_checkpoint.py`、`check_native_baseline.py`、`check_native_models.py` 三份一次性脚本已从活跃目录删除，可在上述档案的 `scripts/evaluation/` 下查看。它们只适用于当时的 N1 源码及数据快照，不能在当前 27 表与 v2 协议上重跑并冒充基线不变。原兼容、基线与真实模型报告继续保留；当前验收入口见[30 号记录](30_native_remaining_implementation_and_acceptance.md)。Windows 沙箱若阻止 asyncio 所需本机套接字，应在允许本机运行的环境执行，不能把权限错误当作测试失败的产品原因。

下一阶段是 N2：真实 `@tool`、ToolRuntime、唯一 messages、根请求预算和实际摘要中间件。N3 的生产持久会话、typed event／wait_id、幂等发题／消息回执及过时恢复拒绝仍未交付，本批 SQLite 探针不能替代这些验收。
