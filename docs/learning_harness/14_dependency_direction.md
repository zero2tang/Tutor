# 14 · 单向依赖与运行依赖注入

当前代码的组织目标是：领域规则不因调用 Agent 而反向导入应用编排，底层客户端不需要知道教学业务，原生图可以组合它们。

## 1. 允许的方向

| 来源 | 允许依赖 |
|---|---|
| shared | 无本项目上层依赖 |
| infrastructure | shared |
| agent | shared |
| domains | agent 中性契约、infrastructure、shared |
| application | domains、agent、infrastructure、shared |
| api | application、domains、agent、infrastructure、shared |

具体规则在[依赖审计](../../scripts/maintenance/audit_backend_dependencies.py)。局部 import 和可解析 import_module 也参与检查，移动导入位置不能绕过架构。

## 2. 领域怎样使用原生子图

PracticeService 接收 generation_graph，ExerciseBatchService 接收 batch_graph；应用层装配具体 LangGraph 图。领域保留质量、范围、幂等和发布事务，避免反向 import application。

同理，领域依赖 AgentRunner Protocol，应用层提供唯一具体 Runner。模型、提示、工具和输出校验不需要领域知道标准 Agent 工厂内部细节。

## 3. 编译对象与本次运行

静态角色工厂可以缓存编译结果。学生、session、turn、模型输出校验器、公开回调和锁属于 RuntimeContext；不得由共享图闭包捕获。

工具通过 ToolRuntime 取得可信范围，子 Agent 明确传父 RuntimeContext 和完整 RunnableConfig。框架负责模型循环与 ToolMessage 回填，项目不操作其私有 ContextVar。

## 4. 数据库事务与回调边界

数据库 Session 只在短事务中存在，不进入 checkpoint。正式领域效果与对应回执同事务保存；模型请求在事务外。公开事件在正式事务后发送，发送失败不能撤销已经存在的原答或证据。

中间件可以检查权限、预算、来源和公开投影，但不能把整个旧执行器迁入其中。生成质量图的普通函数也不能在没有 owner 的情况下直接写库。

## 5. 如何验证

运行[test_backend_dependencies.py](../../backend/tests/test_backend_dependencies.py)与[test_backend_layout.py](../../backend/tests/test_backend_layout.py)。静态结果不能证明恢复正确；另用真实 PostgreSQL、故障注入和新进程框架恢复测试验证运行时边界，SQLite 仅作隔离测试夹具。

[30 号记录](../stem_v1/30_native_remaining_implementation_and_acceptance.md)分别保留隔离、真实 provider、实际迁移和成本数据，避免把“结构清晰”转换成未经测量的性能结论。
