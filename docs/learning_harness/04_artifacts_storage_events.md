# 04 · 契约、正式回执与存储

当前关键区别是：结构化模型输出只是候选，领域事务才产生正式效果，checkpointer 保存执行接续。

## 1. 输入与输出

[v2 输入协议](../../backend/app/application/learning/protocol.py)复用领域请求类型，并由 kind 判别。支持 message／followup／clarify／change_goal、answer、batch_answer、practice_existing、guidance_action、skip。后几类使用明确业务字段，不能转成自然语言后让模型猜操作。

新写请求使用 request_id。作答和依赖当前停点的操作携带 wait_id，单题另有 question_id，整组另有 batch_id。讲题继续检查工作区 expected_version。显式偏好采用 exclude_unset 保留，不能把省略的默认认知类型误当成学生明确要求。

Plan、Lesson、ExerciseDraft、Diagnosis 等契约由[领域 Schema](../../backend/app/domains/learning/schemas.py)定义。严格字段校验之后，仍需验证目标、产物、来源和当前权限。模型 JSON 合法不等于业务可以提交。

## 2. 五类身份不能混用

| 身份 | 用途 |
|---|---|
| session_id／稳定 thread_id | 定位业务会话与图线程；线程名不是授权凭证 |
| request_id | 一次逻辑学生输入，绑定服务端计算的输入指纹 |
| turn_id | 一次实际执行尝试，重启后需重新 claim |
| wait_id | 一个正式题目／题组／讲题／澄清停点 |
| operation_id | 稳定副作用身份，防止发布或生成重复产物 |

run_id 用于运行审计，tool_call_id 用于模型协议对齐。两者都不能替代稳定操作身份。[统一身份原语](../../backend/app/shared/identity.py)不依赖一次随机执行。

## 3. 正式回执为什么独立于 checkpoint

[receipts.py](../../backend/app/application/learning/receipts.py)把 received／completed／abandoned 存在原有 Message metadata 中。完成回执引用正式产物；只保存输入不能视为任务已经完成。

题目生成回执复用 Question.validation，题组发布回执与出题消息同事务写入。原答评分复用 Attempt／BatchSubmission 的幂等边界。相同操作同输入回放原结果，异输入冲突；失败后累计预算不能因换 owner 清零。

SQLAlchemy、PostgreSQL Saver 和 Store 分别管理事务。正式效果已提交、checkpoint 尚未完成时，重入先核对正式回执；不能重做评分或重新发布。记忆正文预写后由业务事务发布指针，未发布文档不可见。运行日志不能代替业务提交凭证。

## 4. 存储分别保存什么

| 存储 | 保存对象 |
|---|---|
| PostgreSQL 业务表 | 会话、原始消息、冻结契约、评分、证据、目标、记忆治理指针和正式回执 |
| PostgreSQL Saver／Store | 独立 schema 中的官方执行检查点及不可变记忆文档，表数与体积单独报告 |
| Elasticsearch | 可重建的公开文本／向量检索投影 |
| Neo4j | 知识点关系及图扩展投影 |
| 有界内存队列 | 当次 SSE 临时帧 |

旧 context_checkpoints 的 ORM 已退役，物理清理和实际表数须由迁移报告证明。原 ExerciseGenerationAttempt 独立表早已在先前裁剪中退出；生成私有诊断现在属于 AgentRun.private_diagnostics，不应继续指向不存在的模型文件。

## 5. 公开边界

冻结内部契约含标准答案和解析；公开题面通过白名单投影。公开流只抽取允许的 JSON 正文字段，业务工具参数、模型隐藏推理和私有答案不直接透传。临时 text_delta 可被 text_reset 撤回，message 才表示正式发布。

SSE 不逐帧落库，无 Last-Event-ID token 续传。断流后先读正式会话快照，而非假设请求失败并重新生成。诊断轮转日志可显式开启，属于观测材料，不是恢复账本。
