# 12 · 服务职责与删除审计

判断一个文件是否冗余，先确定业务对象、调用者、状态所有者和失败恢复边界。命名为 legacy 或静态未被在线入口引用，都不足以单独证明可以删除。

## 1. 当前职责地图

| 层次 | 负责什么 | 典型入口 |
|---|---|---|
| HTTP／SSE | 请求校验、公开传输和临时通道 | [api/streaming.py](../../backend/app/api/streaming.py) |
| 学习用例 | 唯一执行入口、图分支、委派、正式发布 | [learning/entry.py](../../backend/app/application/learning/entry.py) |
| 原生 Agent 接入 | 角色工厂、真实工具、摘要与预算边界 | [agent/graph.py](../../backend/app/application/agent/graph.py)、[tools.py](../../backend/app/application/agent/tools.py) |
| 领域规则与事务 | 题目、评估、证据、目标、记忆和课程事实 | [domains/](../../backend/app/domains) |
| 外部客户端 | 标准模型、ES、Neo4j、向量和 HTTP | [infrastructure/clients/](../../backend/app/infrastructure/clients) |
| 中性辅助 | 稳定身份、预算、并发和执行错误 | [shared/](../../backend/app/shared) |

## 2. 哪些文件已经退出

本批旧执行器、工具桥、手写 Schema、私有调用身份、独立消息控制器、摘要 Store 和请求内作答图已物理退出，完整清单见[30 号记录](../stem_v1/30_native_remaining_implementation_and_acceptance.md)。

旧业务保护迁到明确所有者后才删。比如来源分块留在真实摘要中间件，课程范围留在真实工具／领域服务，取消与用量留在唯一 Runner，正式效果回放留在原有领域记录。

## 3. 普通函数为什么不图化

目录查询、题面投影、help snapshot、证据计算、掌握度归约是确定职责的业务函数。只有昂贵候选、正式事务、分支／并发或等待点才需要节点。

把函数都包装成 Service→Factory→Repository，或把旧执行器改名移入 middleware，不会自然减少复杂度。应检查入口数量、消息副本、工具声明和回执所有者，而不只数文件。

## 4. 审计工具怎样使用

[布局审计](../../scripts/maintenance/audit_backend_layout.py)识别在线、离线、命令和测试引用；[依赖审计](../../scripts/maintenance/audit_backend_dependencies.py)检查静态及可解析动态导入、层次违规和强连通环。

删除前应给出：入口调用链、独有规则去向、替代路径、持久引用处理、对应回归及实际文件删除。历史验收报告保留原快照；已过期可执行验收脚本不能继续冒充当前版本检查。

费用统计也按替换、新能力、一次迁移和验证文档分别计数。删除旧框架适配与新增持久恢复不是同一种成本。
