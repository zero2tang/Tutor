# 09 · 学生、课程与知识点递归记忆

当前记忆不是一份聊天摘要，也不只是旧 MemoryItem 错因列表。[MemoryService](../../backend/app/domains/memory/service.py)提供统一门面，知识点观察与作用域聚合仍由领域服务拥有。

## 1. 一条事实怎样变成记忆

```text
正式消息／真实作答／帮助事件
  → MemoryFact
  → 会话知识点观察 SessionConceptObservation
  → concept ScopeMemory
  → course ScopeMemory
  → student ScopeMemory
```

[BusinessMemoryPipeline](../../backend/app/domains/memory/concepts/service.py)提取会话观察，[HierarchicalMemoryService](../../backend/app/domains/memory/scopes/service.py)按需聚合上层作用域。跨层归纳要保留来源、版本和有效状态，不能把讲解内容直接归纳为学生已经掌握。

MemoryItem 是另一类有证据来源的错因记录，不能与作用域语义记忆或 StudentState 混为一张“学生知识表”。

## 2. 谁执行，谁保存进度

既有 MemoryJob、worker、租约与失败重试继续使用，没有增加另一套 LangGraph 记忆队列。本次只将会话提取、概念归纳和上层聚合的三处旧模型出口切到标准结构化调用。

后台归纳与当前学习请求可以相互独立。事实水位和依赖版本决定需要更新哪些记忆，不需要把整个历史重新塞进主 Agent。

## 3. 在线上下文怎样读取

[memory/context.py](../../backend/app/domains/memory/context.py)按学生、课程和当前知识点选择有限正文，并返回来源 manifest。教学任务可以使用有效偏好和学习观察；正式评分采用隔离目的，不能用历史标签替代当前原答。

知识角色的真实工具可以按范围检索记忆。它拿到的是 ToolRuntime 内的可信学生／课程范围，不是模型自己填写的另一个学生 ID。

## 4. 纠正和遗忘怎样影响恢复

作用域状态、来源版本及 student 失效 epoch 在读取、恢复和提交前核对。原生 checkpoint 不能让已经撤销的记忆再次进入可发布结果；旧候选在来源失效后需要拒绝或重新装配。

遗忘不是简单隐藏一行正文；实际依赖和在途任务都可能需要失效。它也不自动重新评价历史掌握证据，语义记忆与能力账本有不同所有者。

## 5. 与会话压缩有什么区别

| 机制 | 回答的问题 | 主要存储 |
|---|---|---|
| 长期语义记忆 | 下次教学应该记起哪些有效观察／偏好？ | PostgreSQL 事实、治理指针与任务，官方 Store 正文 |
| 掌握证据 | 哪个维度有真实独立作答支持？ | 评分账本与 StudentState |
| 会话摘要 | 当前模型怎样在有限窗口继续本会话？ | LangGraph messages／PostgreSQL Saver |
| 流程进度 | 当前教学任务推进到哪里？ | learning_progress 与目标状态 |

当前功能与历史教学效果验收不能互相替代。详细业务记忆设计见[19 号记录](../stem_v1/19_business_memory_implementation.md)，本次框架变化和证据范围见[30 号记录](../stem_v1/30_native_remaining_implementation_and_acceptance.md)。
