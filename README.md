# 理序 · 理科学习系统

基于 LangChain／LangGraph 的本机教学 Agent 应用，保留知识点 GraphRAG、主子目标、递归记忆、习题质量复核、整组作答、语义压缩和 SSE。当前源码已退出旧模型／工具桥、独立消息控制器和请求内作答图。

2026-10-26 已完成 [PostgreSQL 与 LangGraph 记忆架构迁移](docs/stem_v1/32_postgresql_langgraph_memory_implementation_and_acceptance.md)。业务数据库、执行检查点和三级长期记忆文档分别使用 PostgreSQL 业务表、官方 Saver 和官方 Store；三级教学记忆规则保持原有设计。13,493 行教学资源迁移校验通过，旧学生数据保留在完整备份中。完整后端 865 项、前端 15 项及构建通过，真实模型三级记忆、连续摘要与检索回放通过。2026-10-23 原生化的历史实现与验收见[30 号记录](docs/stem_v1/30_native_remaining_implementation_and_acceptance.md)。

## 核心能力

- **教学与检索**：主 Agent 按需调用知识／习题能力，真实工具回执返回原模型继续。课程目录、教材与 GraphRAG 提供有来源的材料。
- **习题与评估**：单题和 1～6 道题组共用生成→盲解→语义复核图，最多并发 3；全部合格后统一发布。原答、冻结题和帮助条件绑定，整组一次评估及事务提交。
- **目标与记忆**：会话内保留主子目标与补救返回；事实先形成知识点观察，再归纳为知识点、课程和学生记忆，支持纠正与遗忘。讲过不等于掌握，语义记忆不直接改能力证据。
- **长会话与恢复**：标准摘要中间件保护完整当前任务；官方 PostgreSQL saver 保存执行位置。记忆任务使用独立 LangGraph，官方 Store 保存不可变正文，业务指针与撤销屏障决定可见版本。稳定回执避免重复发布与评分。
- **公开输出**：普通教学正文可逐段显示；内部答案、工具参数与隐藏推理不透传。通用讲题采用 validated_publication，正文经校验并保存后分段发布，不把这些分片称为上游即时 token。

当前用于力学与有限元基础、小学数学与拓展思维、初中数学一元一次方程。通用讲题支持粘贴新题、教材题和后续追问，逐步引导与真实作答证据是核心业务。

## 直接使用

```powershell
.\scripts\start.ps1
# 不打开浏览器：
.\scripts\start.ps1 -NoBrowser
# 停止脚本创建的前端、后端和本地精排：
.\scripts\stop.ps1
```

网页：[http://127.0.0.1:5173](http://127.0.0.1:5173)；API：[http://127.0.0.1:8008/docs](http://127.0.0.1:8008/docs)。从当前课程进入学习、知识地图、习题检索或记忆面板，保存会话可在对应课程恢复。

SSE 使用 256 项临时队列和 12 秒心跳，断线或慢消费只结束临时流，后台业务继续；页面刷新读取正式快照。没有逐 token 持久日志或 Last-Event-ID 文字续传。

本版本只监听本机，没有账号体系；业务范围校验不能解释为完整多租户认证。当前只支持单个应用 worker，PostgreSQL advisory lock 保护同一业务库的恢复执行权。

## 安装与配置

后端 Python 3.12，前端 Node.js 20.19+。LangChain 1.4.7、LangGraph 1.2.16、langchain-openai 1.7.2 和 PostgreSQL Saver／Store 3.2.0 按锁文件安装。

```powershell
python -m venv backend\.venv
backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.lock.txt
Set-Location frontend
npm ci
```

默认共享配置来自 `E:\projects\contract\contract-compliance\backend\.env`，可用 TUTOR_SHARED_ENV 改为其他文件；项目覆盖项放 backend/.env，进程变量优先。密钥不写入源码、浏览器或日志。

| 配置 | 用途 |
|---|---|
| TUTOR_DB_HOST/PORT/USER/PASSWORD、TUTOR_DATABASE | 本项目 PostgreSQL，默认 127.0.0.1:5432/stem_tutor |
| ES_URL、可选 ES_USERNAME/ES_PASSWORD | Elasticsearch 公开文本与向量投影 |
| TUTOR_NEO4J_URI/USER/PASSWORD | Neo4j 知识点关系 |
| DEEPSEEK_BASE_URL/API_KEY、LLM_MODEL | 标准多模态聊天模型 |
| DASHSCOPE_BASE_URL/API_KEY、EMBEDDING_MODEL/DIM | 向量模型与维度 |
| TUTOR_EXERCISE_RERANK_ENABLED、TUTOR_EXERCISE_RERANK_URL | 启用习题精排并指定本机 Qwen 地址，例如 `http://127.0.0.1:8019` |
| TUTOR_EXERCISE_QWEN_RERANK_LIMIT | 精排候选数，默认 20；与页面请求的返回条数分开 |
| TUTOR_CONTEXT_* | 部署窗口、输入／输出安全量、图片预留与摘要策略 |
| TUTOR_DIAGNOSTIC_LOG | 可选有上限的观测日志，不是恢复账本 |

首次部署先执行 `backend/.venv/Scripts/python.exe scripts/maintenance/prepare_postgres.py` 建库、升级 Alembic 并初始化官方 Saver／Store；服务启动只检查结构版本。已有资源迁移使用 `scripts/maintenance/migrate_postgres.py` 的 export、preflight、import、verify 命令。MYSQL_* 仅供离线导出读取原库。

业务表位于 public，检查点位于 langgraph_checkpoint，记忆文档位于 langgraph_store；未启用 pgvector。启动只清理无待办、无未完成输入且图已结束的 30 天冷线程，报告 PostgreSQL 关系存储大小。Windows 启动脚本显式使用 Selector 事件循环以支持 psycopg。

## 教材、题库与知识图谱

Elasticsearch 的教材和题库全文字段使用 IK，索引时用 `ik_max_word`，搜索时用 `ik_smart`。本地安装、数学与物理扩展词典、旧索引迁移见 [IK 配置](deploy/elasticsearch/ik/README.md)。

教材试点保留 OpenStax 原文题和当前 DeepSeek 的多模态小节抽取。中学教材按约定先验证 1.1，再扩展第一章，后续章节不处理。定义与教学模式、原文习题和方法技巧带来源与审核状态保存，未复核产物不混入公开目录；教材未因本轮框架迁移自动全量向量化。

启用本地 Qwen 后，支持课程的 Agent 找题默认走混合检索；题库页面可选择「语义混合搜索」。检索从经过主库校验和去重的候选中，先保留文本向量前 12 道，再用 BM25 补到 20 道；候选不足时用剩余召回结果补齐。纯文本走 Qwen3-Reranker-0.6B，查询或任一候选包含公开题图时整批走 Qwen3-VL-Reranker-2B。精排不可用时保留 RRF 顺序，精确题号查询直接走主库。私有答案与解析不进入精排。

本地精排由启动脚本使用 `data/runtime/qwen_rerank_env/Scripts/python.exe` 启动，权重位于 `data/cache/reranker_models/`。单独启动后端时，可从仓库根目录运行 `data/runtime/qwen_rerank_env/Scripts/python.exe -B -m local_reranker.server --port 8019`。模型与离线对照见 [Qwen 精排实测](evaluations/retrieval/QWEN_RERANK.md)。

课程数据与索引可从已有教学元数据重建：

```powershell
Push-Location backend
.\.venv\Scripts\python.exe -m app.application.commands.courses.import_course --course mechanics 'D:\Python项目\CBEGRec-main\data\Mechanical_Physics'
.\.venv\Scripts\python.exe -m app.application.commands.courses.import_course --course math '..\data\raw\xes3g5m\metadata'
Pop-Location
```

--import-only 只导入 PostgreSQL，--index-only 重建外部投影。完整构建会写本项目数据库与检索服务，必要时调用向量模型；不读取原数据中的历史学生交互。HTTP 教材解析与离线索引是两条不同路径。Neo4j 增量 MERGE 不等于会自动删除全部过时关系。

## 当前调用链与学习文档

```text
HTTP / SSE
  → 唯一 entry：协议、claim、正式回执、wait、目标与记忆版本
  → 会话 StateGraph（7类节点）
  → create_agent / 真实@tool / 必要子图
  → PostgreSQL 领域事务与正式消息
  → interrupt 等待或结束
```

PostgreSQL 业务表保存原答、冻结题、评分证据、目标和记忆治理指针；官方 Store 保存长期记忆正文，Saver 保存图状态。RuntimeContext 每次重新注入身份、服务和预算。三个持久化入口使用独立事务，通过不可变文档与正式回执核对恢复。

从[Learning Harness 指南](docs/learning_harness/README.md)阅读当前组件，重点看[核心元素串讲](docs/learning_harness/15_core_design_elements_and_agent_loop.md)、[源码顺序](docs/learning_harness/06_source_reading_guide.md)、[后端导航](docs/learning_harness/13_backend_structure.md)和[依赖边界](docs/learning_harness/14_dependency_direction.md)。

API 的原 URL 保留，新写请求统一 request_id；单／整组作答和依赖当前停点的动作还需要 wait_id。单题提交携带 question_id，整组携带 batch_id；GET 会话返回 pending_interaction。旧 idempotency_key 请求字段不长期双收，过时停点明确冲突。

## 验证与历史记录

```powershell
# 隔离回归；pytest管理指定临时目录，使用新的专用路径。
backend\.venv\Scripts\python.exe -m pytest backend\tests -q -c backend\pyproject.toml --basetemp=data/tmp/pytest-native-local
Push-Location frontend
npm run test:streaming
npm run build
Pop-Location
```

隔离测试不调用真实 provider；真实模型验收使用独立 evaluation_* 学生与库，会产生调用费用。新 Python 进程中的实际 create_agent 恢复测试使用录制标准模型，属于框架协议证据。

历史事实继续保留：[系统简化阶段 23～27](docs/stem_v1/23_core_system_simplification_plan.md)、[N0／N1](docs/stem_v1/29_native_n0_n1_implementation_and_acceptance.md)、[递归记忆实现](docs/stem_v1/19_business_memory_implementation.md)、[教材抽取](docs/stem_v1/16_textbook_multimodal_extraction.md)、[含图检索](docs/stem_v1/21_multimodal_exercise_retrieval_implementation.md)。不同报告的计数不合并成一次当前完整通过，旧教学失败也不因框架迁移自动变为已解决。

本项目验证工程流程与数据边界，不据此承诺真实学生学习增益、零教学错误、检索泛化指标或多 worker 容量。
