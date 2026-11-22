# 13 · 后端结构与功能导航

本篇是当前源码导航；历史 N0／N1 与本次 N2～N6 的差别见[30 号记录](../stem_v1/30_native_remaining_implementation_and_acceptance.md)。

## 1. 六类目录

```text
backend/app/
├─ main.py / bootstrap.py       应用、生命周期与装配
├─ api/                        HTTP、请求与SSE
├─ application/
│  ├─ learning/                唯一入口、会话图、正式回执和子图
│  ├─ agent/                   标准角色工厂、真实工具、摘要、提示与运行记录
│  ├─ guidance/                可幂等重入的讲题活动
│  ├─ question_retrieval/      含图题目检索用例
│  └─ commands/                离线导入等命令
├─ domains/                    规则、协议、事实与短事务
├─ agent/                      中性运行契约、来源投影、流过滤、工具策略和Skill
├─ infrastructure/             配置、客户端与通用数据库能力
└─ shared/                     稳定身份、预算、并发等无业务依赖辅助
```

Agent 的具体 LangChain／LangGraph 集成在 application/agent；agent 包保留不反向导入领域或数据库的中性协议和保护。没有为了目录名而另建第二个执行内核。

## 2. 九个业务域

| 域 | 事实与规则 |
|---|---|
| learning | Plan／Lesson、学习流程进度 |
| sessions | 会话、正式消息、执行权、主子目标和等待点 |
| practice | 选题、生成质量、冻结契约、题组发布 |
| assessment | 原答评分、评分包、局部证据与整组事务 |
| students | 证据归约、掌握状态和错因记录 |
| guidance | 讲题工作区、帮助曝光、反馈、提交证据 |
| memory | 事实、知识点观察、作用域聚合、纠正和遗忘 |
| courses | 目录、题库资源、难度、检索和知识关系 |
| materials | 教材、解析、抽取、资产和访问投影 |

题库 question 与一次练习 practice 有不同生命周期；课程事实、学习结果和执行 checkpoint 也不能合为同一状态。复用业务能力不等于合并这些所有者。

## 3. 四条阅读链

- 在线学习：[routes](../../backend/app/api/routes/learning.py) → [entry](../../backend/app/application/learning/entry.py) → [session graph](../../backend/app/application/learning/graph.py) → [runner](../../backend/app/application/agent/runner.py) → [真实工具](../../backend/app/application/agent/tools.py)。
- 习题：[质量子图](../../backend/app/application/learning/subgraphs/practice.py) → [practice/service](../../backend/app/domains/practice/service.py) → [batches](../../backend/app/domains/practice/batches.py)。
- 作答：[assessment/service](../../backend/app/domains/assessment/service.py)／[batch](../../backend/app/domains/assessment/batch.py) → [evidence](../../backend/app/domains/assessment/evidence.py) → [students/mastery](../../backend/app/domains/students/mastery.py)。
- 记忆：[memory/service](../../backend/app/domains/memory/service.py) → [concepts](../../backend/app/domains/memory/concepts/service.py) → [scopes](../../backend/app/domains/memory/scopes/service.py)。

教材 HTTP 导入与离线 ES／Neo4j 投影是不同路径。离线命令在 application/commands，不能仅看到解析函数就宣称自动 RAG 索引已经连通。

## 4. ORM 和执行存储

[model_registry.py](../../backend/app/application/model_registry.py)显式登记 27 张业务 ORM 表；旧 context_checkpoints 模型已经移除。2026-10-26 已迁移至 PostgreSQL，Alembic 管理业务结构，官方 Saver／Store 分别管理独立 schema。框架表数与体积单独报告，不能把跨存储移动算作总存储减少。[2026-10-23 的历史迁移](../../data/reports/native_remaining/migration.json)保留原有口径。

新增代码先选事实所有者，再确定是否需要跨域应用用例、真实工具或图节点。当前[依赖规则](14_dependency_direction.md)由测试执行，不靠目录约定口头保证。
