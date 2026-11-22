# Learning Harness 当前实现学习入口

原先的多 Agent 设计稿已按当前项目实现重写。新的入口是：

**[从当前项目学习 Learning Harness](learning_harness/README.md)**

推荐先读 [总体架构](learning_harness/00_overview_and_architecture.md)、[四个 Agent 与委派结构](learning_harness/01_agent_organization.md)、[端到端流程](learning_harness/05_learning_workflows.md) 和 [核心设计思想](learning_harness/07_design_principles.md)，再按 [源码阅读指南](learning_harness/06_source_reading_guide.md) 追踪真实调用链。

文档以 2026-10-20 当前工作区源码为基线，讲解四角色共享执行器、受约束子 Agent 工具、服务端证据提交和学习接续。旧稿中的 Topic Tutor、通用 MemoryService、严格 Mastery Gate 等不再作为当前已实现组件陈述。
