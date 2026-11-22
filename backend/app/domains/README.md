# 业务功能导航

业务域保存自己的规则、交换契约和持久化状态；跨域用例由上层 [application](../application/learning/service.py) 编排。包初始化不装配服务，全部表由[模型注册入口](../application/model_registry.py)显式登记。

| 功能域 | 建议入口 | 主要边界 |
|---|---|---|
| 学习契约与进度 | [learning/schemas.py](learning/schemas.py)、[progress.py](learning/progress.py) | 学习计划与流程进度；实际用例在 [application/learning](../application/learning/service.py) |
| 会话和目标 | [sessions/service.py](sessions/service.py)、[leases.py](sessions/leases.py)、[goals.py](sessions/goals.py) | 会话事实、租约、消息、目标图及恢复 |
| 练习过程 | [practice/service.py](practice/service.py)、[reviews.py](practice/reviews.py)、[batches.py](practice/batches.py) | 选题、生成、盲解、复核、冻结题目及练习组 |
| 题库资源 | [courses/questions/policy.py](courses/questions/policy.py)、[images.py](courses/questions/images.py)、[search/store.py](courses/questions/search/store.py) | 题目可用性、公开题图、来源、复核表示与可重建检索投影 |
| 评估 | [assessment/service.py](assessment/service.py)、[evidence.py](assessment/evidence.py) | 原答评分、冻结评分包、证据归因及账本 |
| 课程 | [courses/catalog.py](courses/catalog.py)、[queries.py](courses/queries.py) | 目录事实、确定性排序、难度和检索；个人画像查询由应用层注入 |
| 学生 | [students/service.py](students/service.py)、[mastery.py](students/mastery.py) | 画像与掌握度归约，消费证据投影，不回调评分服务 |
| 教材与资料 | [materials/textbooks.py](materials/textbooks.py)、[resources/service.py](materials/resources/service.py)、[transfer.py](materials/transfer.py) | 文档解析、教学包、公开资料与可信转交协议 |
| 引导教学规则 | [guidance/workspace.py](guidance/workspace.py)、[feedback.py](guidance/feedback.py)、[publication.py](guidance/publication.py) | 题目身份、帮助曝光、反馈校验与公开内容；用例在 [application/guidance](../application/guidance/service.py) |
| 学生记忆 | [memory/service.py](memory/service.py)、[concepts/service.py](memory/concepts/service.py)、[scopes/service.py](memory/scopes/service.py) | 事实、观察、记忆候选、来源屏障及层次聚合 |

同层域仍会读取彼此的事实模型，并在同一事务内协作；例如学生画像统计 `Attempt`，会话恢复构造公开题目。当前没有要求每个域成为独立服务或独立数据库。学生域 `MemoryItem` 是错因记录，记忆域 `MemoryFact`、`ScopeMemory` 是来源与模型记忆系统，两者职责不同。

题库资源与练习过程的区别、共享检索组件及兼容边界，见[题库与练习职责重构](../../../docs/stem_v1/22_question_and_practice_refactor.md)。

[后端导航](../README.md) · [完整目录](../../../docs/learning_harness/13_backend_structure.md) · [依赖与调用链](../../../docs/learning_harness/14_dependency_direction.md)
