# 10 · 会话、暂停恢复与 SSE

恢复必须先说明恢复哪一种状态：正式学习结果、图执行位置和浏览器临时文字是三个不同层次。

## 1. 唯一入口的处理顺序

[entry.execute](../../backend/app/application/learning/entry.py)先校验事件并 claim，再重读正式输入、完成回执、当前 wait、目标版本、记忆 manifest 和失效 epoch。thread_id 稳定为 learning:{session_id}，但 thread 不能代替身份和 owner。

相同请求已完成先回放；相同请求未完成且执行栈仍有效时继续原线程。只有匹配正式业务等待点的新输入才使用 Command(resume=...)，不能把原事件当新输入再从 START 运行。

新输入终止已释放执行权的旧计算时，旧请求可标 abandoned。原始答案和已提交效果继续保留，旧请求不能随后重新发布过时候选。

## 2. 为什么需要两个提交边界

PostgreSQL 业务事务提交题目、评分、证据和正式回执，官方 PostgreSQL Saver 保存原生图位置。连接相同数据库也不代表共享事务：

\[
\text{领域提交成功} \centernot\Rightarrow \text{后续 checkpoint 已保存}
\]

所以稳定 operation_id 和正式回执必须在效果事务中产生。重入先找正式效果，不能因为 checkpoint 缺少“已完成”就重新评分或生成。

prepare_assessment 保存昂贵候选，commit_assessment 负责事务。候选恢复后仍要核对原答、冻结契约、帮助快照、当前 owner 和记忆／目标边界。

## 3. 等待点如何保护原题

[pending_interaction](../../backend/app/domains/sessions/interactions.py)从正式题目消息、题组或工作区计算 wait_id。它与 LangGraph 本次 interrupt ID 分开。

已答或跳过旧题、切换题组、关闭讲题工作区后，旧 wait 失效。同题追问不应自动变成答案，但真实帮助条件必须更新；随后评分读取最新且与提交绑定的快照。

await_student 是独立无副作用节点。interrupt 恢复可能重新进入节点，所以它不能先发题或扣预算。

## 4. 记忆纠正、遗忘和安全重建

来源 manifest 与 student 失效 epoch 都来自正式记忆状态。在途候选及恢复消息必须重新验证。来源已撤销或目标版本不兼容时，旧执行状态不能继续发布；系统从正式事实支持的安全入口重建，复用已提交产物。

检查点丢失时可以从正式事实恢复待答题和结果，不能声称恢复了不存在的中间推理。未提交模型计算可以丢弃，真实原答与正式证据不能丢弃。

## 5. 存储与进程边界

[storage.py](../../backend/app/application/learning/storage.py)管理官方 AsyncPostgresSaver、显式类型反序列化和应用生命周期。数据库 advisory lock 约束同一业务库的单 worker 恢复执行权，等待学生不保留后台阻塞协程。

30 天无业务更新、无待答或未完成请求且图无后续步骤的冷线程可以整线程清理。活跃线程不自动清理；维护报告统计 PostgreSQL 框架表行数和关系存储大小，不承诺删除后立即归还物理空间。

## 6. SSE 能恢复什么

[api/streaming.py](../../backend/app/api/streaming.py)使用容量 256 的临时队列，空闲 12 秒心跳。慢消费者或断线关闭临时流，后台任务继续保存正式结果；前端读取会话快照并恢复请求身份。

没有逐 token 日志、Last-Event-ID 文字续传或事务 Outbox。取消应用任务与断开浏览器不是同一操作；应用退出会取消并等待在途任务收尾。

实际 create_agent 新进程恢复与领域故障测试见[实施记录](../stem_v1/30_native_remaining_implementation_and_acceptance.md)。本机单 worker 结果不能外推为分布式 lease、抢占或学校级并发保证。
