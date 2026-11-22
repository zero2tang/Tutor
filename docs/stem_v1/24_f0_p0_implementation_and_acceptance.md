# F0 与 S01～S04 实施及验收

日期：2026-10-23。状态：**本阶段完成；P1／F1 及以后阶段尚未实施**。

执行依据：[统一简化计划](23_core_system_simplification_plan.md)。用户已明确当前数据库为实验数据，允许修改。本轮保留六项核心能力，没有把线上 Agent 切换到 LangChain；框架仅在 F0 隔离验证中调用。

## 1. 本阶段实际完成的改变

| 项目 | 最终实现 |
|---|---|
| F0 | 在 `.tmp/f0-deps` 隔离安装并真实调用当前 `deepseek-flash`；锁定依赖，验证文字、教材页图、工具、结构化输出、框架循环／进程内检查点、流式、SDK 超时、取消和错误 |
| S01 | 删除 `ExtractedResource` 和提取提交时的拆分资源写入；三类内容与教学模式仍存于 `ExtractedSection.payload`，正式题目／资料发布继续保留 |
| S02 | 删除 `QuestionMethodReference` 和 `method` CLI；教材方法提取、方法信息和 `same_method` 检索意图保留 |
| S03 | 删除 `RunEvent` ORM、逐帧 SQL 写入和历史读取；旧 `/events` 返回 410。SSE 使用最多 256 项的队列，断线停止投递，满缓冲结束临时流，后台继续提交正式结果 |
| S04 | 删除旧辅导模型与实时统计模块；历史统计固定为带版本的文件快照，旧观察迁入事实表，旧会话转换为公开原题入口，由普通 `/guidance` 接续 |

可选 `TUTOR_DIAGNOSTIC_LOG` 只保存预算、用途、记忆加载和模型指标，最多 8 MiB 加一份轮转备份；默认不启用文件保存，不保存正文／完整工具结果，不承担恢复。需要逐请求指标的旧验收脚本已迁到这个明确启用的日志来源，不再依赖 SQL 事件表。

## 2. 数据库实际由 49 表变为 44 表

| 已物理删除的表 | 删除前行数 | 数据去向 |
|---|---:|---|
| `textbook_extracted_resources` | 395 | 逐项核对小节 payload 与内容哈希，完整归档 |
| `exercise_search_methods` | 0 | 空表退出，无线上消费逻辑 |
| `run_events` | 9,231 | 原诊断归档；新临时帧不保存 SQL 历史 |
| `tutoring_actions` | 120 | 原请求／原答／回执完整归档 |
| `tutoring_evidence` | 120 | 原记录归档；同学生／会话的观察保留在 `memory_facts`，模拟标记不改成正式掌握证据 |

五表共 9,866 行均在 MySQL 临时表中实际还原，并核对 DDL、逐行内容与哈希；不是只备份行数。删除后连续两次执行正常 `initialize_database()`，仍为 44 表，退役模型、建表登记和时间精度迁移均不再引用这些表。

归档在 `data/archives/simplification_p0/20261023/`，包括 `retired.json.gz`、`source_before.zip` 和 `student_summaries.json`，已加入 Git 忽略。归档文件仍占空间，不将退表解释为立即回收全部磁盘空间。

### 数据修改明细

- 395 个拆分资源全部能在原小节中按来源 ID 找到同内容。
- 120 条旧辅导观察全部有对应的记忆事实；其中 1 条原已存在，本次新增 119 条。保留原始发生时间、来源、原观察、独立／帮助条件和模拟标记，不更新学生掌握度。
- 生成 15 个学生的固定历史摘要；在线读取不再扫描旧表。缺少档案文件时返回“无旧摘要”，不伪造零分或已评估状态；正式作答和能力状态不依赖该文件。
- 17 个旧会话移除 `equation_tutoring` 状态解释，保留公开 `archived_problem`，原始状态完整归档。题干保持原变量，内部参考答案不进入公开字段；保留 `_tutoring_order` 消息顺序语义。
- 真实路由验收新增 1 个 `evaluation_*` 学生会话、2 条消息、1 个角色运行和 1 个目标树；这两条消息同时登记了记忆事实。

| 发生内容变化的保留表 | 本轮前 | 本轮后 | 原因 |
|---|---:|---:|---|
| `learning_sessions` | 124 | 125 | 转换 17 个旧会话，另新增 1 个工程验收会话 |
| `memory_facts` | 89 | 210 | 119 条旧观察＋2 条验收消息事实 |
| `memory_session_states` | 15 | 30 | 事实序号与水位登记 |
| `messages` | 739 | 741 | 真实讲题请求和正式反馈 |
| `agent_runs` | 634 | 635 | 真实讲题运行 |
| `session_goal_states` | 22 | 23 | 新验收会话目标 |

其余 38 张保留表内容哈希一致，包括 7,973 道题、48 次原答、32 条能力状态、3 条整组提交、2,362 条知识关系、37 个压缩缓存和教材模型回执。没有重跑教材后续章节、重建索引或把模拟旧观察变成新的真实评分。

## 3. 实际文件与代码量

以本轮开始时的 `before.json` 与 `source_before.zip` 为基线，不用包含之前大量未提交重构的 Git 总 diff 作为本次统计。行数包含注释和空行。

### 完整删除的生产文件

| 文件 | 删除行数 | 承接方式 |
|---|---:|---|
| `backend/app/domains/students/legacy.py` | 74 | 固定历史快照＋普通讲题入口，不保留旧实时统计或专用状态解释 |
| `backend/app/domains/students/legacy_models.py` | 40 | 两张历史表已归档并物理删除 |

新增 [students/archive.py](../../backend/app/domains/students/archive.py) 19 行，仅负责读取固定摘要。三个嵌入现有文件的冗余 ORM 类及旧 CLI／事件／续接分支也已删除；不能将这些局部删除算成“完整删除文件”。

| 生产源码统计范围 | 文件数前→后 | 行数前→后 | 净变化 |
|---|---:|---:|---:|
| `backend/app` Python | 237→236 | 24,027→23,828 | **−199** |
| `frontend/src` 文本源码 | 29→29 | 6,440→6,442 | **＋2** |
| 合计 | 266→265 | 30,467→30,270 | **−197** |

测试文件 `test_legacy_tutoring.py` 已退出，由 `test_archived_learning.py` 验证课程初始化、固定历史展示、模拟证据和争议隔离；它不计入两个生产文件的删除量。

另新增离线工具：`simplify_p0.py` 232 行、`check_agent_f0.py` 199 行、`check_simplification_p0.py` 120 行、`diagnostic_events.py` 27 行，合计 **578 行**。这些是迁移与验证代码，不进入服务启动链；其他既有验收脚本也有适配。因此本阶段只能确认生产源码减少 197 行，**不能将它表述为全仓库总代码净减少**。逐文件生产差异在 [change_summary.json](../../data/reports/simplification_p0/change_summary.json)。

## 4. F0 的真实兼容结论

隔离锁定的主要版本：LangChain 1.4.7、LangGraph 1.2.16、langchain-openai 1.7.2、OpenAI SDK 3.37.0、Pydantic 2.14.1。完整解析结果共 44 个包，见 [F0 锁定文件](../../backend/requirements-agent-f0.lock.txt)；包含已有依赖的重叠包，不能解释为生产新增了 44 个依赖。生产 `requirements.txt`／现有虚拟环境未切换。

| 验证 | 结果 |
|---|---|
| 文字＋回传用量 | 通过 |
| function calling 结构化输出 | 通过 |
| JSON mode | 提示明确要求 JSON 后通过；首次缺少该提示返回 400，已修正探针后重验 |
| 原生 JSON Schema | **当前部署明确返回 400：该 response_format 不可用** |
| 原教材页图＋结构化识别 | 通过：体重增长值提取为 2、−1、0；图片原件哈希写入报告 |
| `create_agent` 真正调用工具并回到模型 | 通过：工具计算 17×19，最后返回结构化 323；进程内检查点结束状态可读取 |
| 正文增量和流式用量 | 通过 |
| 无效模型错误、SDK 超时、请求取消 | 通过；不自动无限重试 |
| 请求关闭思考参数 | 在真实出站请求上核对为 disabled |

最近一轮共 9 项通过，1 项已确认不支持；11 次出站请求。成功响应回传 2,195 输入 Tokens、197 输出 Tokens；这不是所有初始失败／前轮探针和取消请求的完整计费统计，也不据此推断容量或稳定性能。

F1 必须显式选择 `ToolStrategy`／function calling，或带明确 JSON 提示的 JSON mode，不默认依赖原生 Schema。评分仍不开放业务工具或历史记忆；用于输出格式的 Schema tool 不等于允许评分读取教学工具。F0 已核对新版异常子类，F1 接入时继续映射到项目错误协议并跑完整依赖回归。

报告：[f0_model_compatibility.json](../../data/reports/simplification_p0/f0_model_compatibility.json)。前轮报告保留，当前线上仍使用原自定义 Agent 循环。

## 5. 验收证据与边界

- 最终后端完整回归：**790 passed**，见 [backend_final.xml](../../data/reports/simplification_p0/backend_final.xml)。首轮的两处旧脚本导入失败已经修正，既保留首轮记录，也完成了最终整轮重跑。
- 前端流式／恢复：**15 passed**；前端生产构建通过。
- 真实 MySQL＋当前模型＋ASGI 路由：教材 1.1 公开读取正常（5 个定义、15 道习题、2 个方法），答案／解析不公开；新讲题产生真实正文、正式消息可刷新，重复请求不重复生成消息，旧事件接口返回 410。
- 有界 SSE 专项：正文早于操作完成、失败关闭、非流式不写事件、客户端断开后继续执行、队列满不阻塞业务，均通过。真实 SDK 的网络增量由 F0 验证；ASGI 集成不冒充浏览器网络时序测量。
- 归档还原与两次初始化：五张表还原一致，44 表保持不变。

六项核心能力继续由原有专项测试覆盖：递归记忆／纠正遗忘、知识点 GraphRAG／课程范围、主子目标／补救返回、并发习题组／整组事务、语义压缩／当前原答完整和 SSE／断线恢复。图关系、正式原答、能力、组契约和压缩缓存还做了数据库内容哈希检查。本轮没有重新开展六项能力各自的长程真实模型实验，也不声称验证了真实学生学习增益。

综合报告：[acceptance.json](../../data/reports/simplification_p0/acceptance.json)；归档验证：[archive_verification.json](../../data/reports/simplification_p0/archive_verification.json)；迁移事实：[migration.json](../../data/reports/simplification_p0/migration.json)。

## 6. 下一阶段

本次停在 F0／P0 验收点。按统一计划，下一阶段为 P1 的 S05～S09 实体与状态合并；F1 在表结构稳定窗口替换 Agent 循环。后续仍以真实删减与六项能力验收为条件，不在本阶段顺带更换生产底座或开始后续章节。
