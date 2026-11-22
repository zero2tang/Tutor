# 08 · 基于 LangChain 的模型循环与工具执行

当前循环由 create_agent 提供。项目没有另一套 wire 消息推进器，也没有占位工具配合 middleware 绕开 handler 的生产路径。

## 1. 一次角色调用

[AgentRunner.run](../../backend/app/application/agent/runner.py)依次准备角色上下文、建立运行记录、选择标准 Agent 或纯结构化模型、等待合法结果、执行最终来源检查并记录用量／错误。

[create_role_agent](../../backend/app/application/agent/graph.py)按静态配置缓存编译图。学生、会话、任务、校验器和公开展示器都在本次 RuntimeContext；不能捕获在共享编译闭包中。checkpointer=None 允许被父图调用时继承持久执行栈。

## 2. 模型—工具—模型

标准模型返回真实 function call，框架执行对应 @tool，结果按 tool_call_id 成为 ToolMessage，原模型继续。最终 ToolStrategy 结构化结果经过业务校验，才能成为可发布候选。

工具声明来自[tools.py](../../backend/app/application/agent/tools.py)中的类型与函数，不另维护 definitions.py。TeachingTools 只装配领域能力与角色工具集；具体函数直接调用目录、检索、记忆或委派服务。

## 3. Harness 中间件保留什么

[TeachingBoundaries](../../backend/app/application/agent/middleware.py)处理业务边界：真实角色权限、参数与范围、调用预算、工具故障熔断、公开投影和模型请求预算。它调用标准 handler，不接管框架模型循环和工具回填。

原生 aafter_model 钩子可以要求修正动态业务输出；修复次数有限。网络错误、取消、执行权丢失不能伪装为普通参数修正。质量拒绝与基础设施执行错误分别计数，连续失败工具会熔断。

Skill 正文只是方法资源；读取 Skill 不增加工具权限。图片读取也必须通过资产范围和公开条件检查。

## 4. 并发有两个层次

只读工具可以有界并发，结果仍按调用身份对齐。同一模型轮的写入型委派按运行锁顺序执行，防止恢复身份依赖并发进入顺序。父子调用通过显式 RuntimeContext 共享根预算，子调用拥有自己的锁，避免父等子死锁。

多题并行在 Send 子图中完成。它处理最多 6 道题、并发上限 3；任一失败后等待兄弟任务取消收尾，避免释放会话后还有迟到写入。

## 5. 输出流为何还需要薄适配

公开事件监听标准模型回调，只读取最终输出函数的白名单字段。业务工具参数、私有答案和隐藏推理不被直接转发。失败会撤回该运行的公开草稿，正式消息通过领域发布后保存。

保留 JSON 字段解析器是公开协议需要，不能为了减少文件就直接公开全部模型内容。完整 messages／State 也不能直接成为 API 响应。

## 6. 如何回答面试中的 harness 问题

可以沿实际链解释：LangChain 管标准工具循环与结构输出，LangGraph 管业务分支和恢复；项目自己的 harness 将可信身份、根预算、来源失效、领域幂等和公开流接到这些扩展点。然后用一次提交后中断展示正式回执和 checkpoint 分别保护什么。

不能仅列框架 import 数量。实现证据与真实 provider 结果须分别引用[30 号记录](../stem_v1/30_native_remaining_implementation_and_acceptance.md)。
