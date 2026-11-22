# 题库与练习职责重构

更新：2026-10-22。

这次重构解决三类实际重复：新旧检索各自构造过滤、召回请求和融合；目录、索引、检索与选题各自判断题目可用性；讲题上下文与含图检索各自读取和校验题图。不同目录出现 exercise 不全是重复业务，其中也包含 HTTP 适配、跨域编排、资源事实和学生练习状态等合理分层。

本次在现有工作区上修改，保留此前未提交的工作。变更前保存了源码快照和接口契约，随后完成职责迁移、共享组件提取与回归验证。没有重建业务数据库或删除学习记录。

## 1. 先区分题库资源和练习过程

同一道题可以出现在多个学生、多个会话的练习中。题库资源保存题面、来源、版本、配图和检索表示；一次练习保存本次选择、生成复核、冻结契约、作答及帮助状态。两个对象的生命周期不同，不能合成一个不断扩大的 exercise 服务。

| 当前目录 | 职责 | 主要入口 |
|---|---|---|
| `domains/courses/questions` | 题库资源的公开范围、可用性和题图关联 | [policy.py](../../backend/app/domains/courses/questions/policy.py)、[images.py](../../backend/app/domains/courses/questions/images.py) |
| `domains/courses/questions/search` | 资产、来源、复核表示、索引投影、构建与检索回执的持久化 | [store.py](../../backend/app/domains/courses/questions/search/store.py)、[representation.py](../../backend/app/domains/courses/questions/search/representation.py) |
| `domains/practice` | 选题、生成、独立求解、语义复核、冻结与练习组 | [service.py](../../backend/app/domains/practice/service.py)、[context.py](../../backend/app/domains/practice/context.py)、[batches.py](../../backend/app/domains/practice/batches.py) |
| `application/question_retrieval` | 读图、构建、来源检查、图文检索和重排的编排 | [service.py](../../backend/app/application/question_retrieval/service.py)、[build.py](../../backend/app/application/question_retrieval/build.py) |
| `api/routes/question_retrieval.py` | 从可信会话取得范围，提供检索与公开题图接口 | [question_retrieval.py](../../backend/app/api/routes/question_retrieval.py) |

原 `domains/exercises` 迁入 `domains/practice`；原 `domains/materials/exercise_search` 迁入 `domains/courses/questions/search`；原 `application/exercise_retrieval` 迁入 `application/question_retrieval`。内部服务分别使用 `PracticeService`、`QuestionRetrievalService`、`QuestionSearchStore`。Question 仍在课程公共 ORM 模型中，避免为整理名称再拆分既有表和事务关系。

教材解析与小节教学包继续由 `domains/materials` 管理。教材习题成为 Question 后，其资源可用性与检索表示采用题库规则；教材原页、教学包与题目索引不是同一个对象。

## 2. 检索共享什么，分别保留什么

当前调用链：HTTP / Agent 工具 → `RetrievalService` → 按资源类型与课程选择普通材料检索或 `QuestionRetrievalService` → 共用召回组件 → 各自校验与投影。

| 共用组件 | 收敛内容 |
|---|---|
| [retrieval/scope.py](../../backend/app/domains/courses/retrieval/scope.py) | 课程、公开可见性、知识点 any/all、题型、排除集合和候选全集约束；数据库降级使用对应的范围匹配 |
| [clients/retrieval_index.py](../../backend/app/infrastructure/clients/retrieval_index.py) | 精确 ID、BM25、向量查询的 ES 请求封装与 embedding 字段排除 |
| [retrieval/channels.py](../../backend/app/domains/courses/retrieval/channels.py) | 并行执行、逐通道计时、错误分类与成功结果保留；取消向上游传播 |
| [retrieval/fusion.py](../../backend/app/domains/courses/retrieval/fusion.py) | 唯一的 RRF 实现及通道内去重 |

RRF 对结果对象 $d$ 的得分为：

$$
S(d)=\sum_{c\in C(d)}\frac{1}{k+r_c(d)},\qquad k=60
$$

$r_c(d)$ 是对象在通道 $c$ 中的名次。材料检索以材料文档为对象，保持原有精确题号优先和同分稳定顺序；题目检索以 question_id 为对象，同一道题的多个文本分块和配图在同一通道只贡献一次名次。若把材料也按 question_id 合并，两篇关联同题的不同讲解会被错误折叠，因此这是参数化的业务差别。

普通材料检索保留 Graph RAG 扩展、材料字段权重与数据库降级。含图题目检索保留资产来源和版本检查、编码器兼容、内容身份排除、多模态召回、重排及持久化回执。共享基础组件没有把两条业务流程硬合并，也没有改变向量空间和已有索引版本。

## 3. 题目能力采用同一规则

[questions/policy.py](../../backend/app/domains/courses/questions/policy.py) 返回明确的能力判断，调用方按用途选择。公开题面、独立练习和内部解析具有不同条件，不能只用一个 visible 布尔量代替。

| 资源状态 | 公开题面 | 共享检索 | 独立练习 | 内部答案、解析 |
|---|---|---|---|---|
| 公开、ready 的原题 | 可以 | 可以 | 可以 | 需要服务端授权 |
| 缺图、选项异常或 reference_only | 可以，附状态说明 | 可以，作为参考 | 不可以 | 需要服务端授权 |
| private、source_incomplete 或未知状态 | 不提供新公开读取 | 不可以 | 不可以 | 只有明确允许历史读取的内部流程可取 |
| 复核通过、ready 的生成题 | 保持原公开题面契约 | 不进入共享题库检索 | 可以由原有练习流程使用 | 需要服务端授权 |

目录读取、离线索引、新旧检索与故障降级、难度目录、练习选题调用同一套规则。需要 SQL 分页的入口使用同模块的 SQL 条件；测试对照 SQL、Python 与离线投影的可检索集合。

修复了撤回状态可能被“缺图”判断覆盖的问题：source_incomplete 不再因为同时缺图而变成可检索资源。提供复核题图不会把 reference_only 自动升级为独立评分题；模型在数据中填写 solution_authorized 也不能获得解析授权。

公开题目、内部题解、冻结练习仍保留各自数据投影。历史评分和已冻结练习依据保存的契约恢复，不通过新资源可用性判断重新生成或改写旧题。

生成题另移除了一个多余副作用：复核通过并保存后，不再调用 embedding 和共享 ES 写入。共享检索已经排除 generated 题，这次写入既无助于当前查题，又可能让成功生成的练习因索引服务故障而报错。生成、盲解、复核、审计和保存流程继续执行；新增故障测试验证此路径无需共享检索服务。

## 4. 题图复用校验，各自保留预算

[questions/images.py](../../backend/app/domains/courses/questions/images.py) 统一解析公开题干、选项和 images 中的受控引用，并保留选项字母、多处字段关联。答案、解析配图与任意外部地址不作为公开题图来源。

[QuestionImageReader](../../backend/app/infrastructure/documents/question_images.py) 统一校验实际路径范围、单图大小、文件格式与完整性、像素数量和内容哈希；检索可计算原有归一化图像身份，讲题读取可以跳过身份计算。教材资产通过 [public_asset_pixels](../../backend/app/application/question_retrieval/assets.py) 再校验题目、课程归属、公开角色和当前绑定。

[讲题上下文](../../backend/app/application/agent/images.py) 只保留上下文范围、去重和消息组装。讲题的 16 张 / 12 MiB 与检索的 10 张 / 12 MiB 分别使用独立预算对象；单图保持 2 MiB 上限。共用校验不会让一次检索消耗另一次讲题的预算。

本次也发现旧测试中的一张 Base64 PNG 校验码损坏，已替换为有效图片，并增加损坏 PNG 在共享读取器和讲题上下文中均被拒绝的回归。

## 5. 兼容与运行入口

外部 HTTP URL、请求响应 Schema、Agent 角色与工具名、`TUTOR_EXERCISE_*` 环境变量、数据库表名、原有索引名及报告目录保持原契约。没有为了内部命名统一而迁移学习状态或更名数据库表。

内部 Python 导入路径和服务装配键已更新；仓库内代码、测试和维护脚本一起迁移，不再保留一套旧实现。自行维护的外部脚本若使用旧内部包路径，需要改用当前路径。离线命令改为：

```powershell
$env:PYTHONPATH = "backend"
backend/.venv/Scripts/python.exe -m app.application.question_retrieval.cli --help
```

表示版本仍是 `exercise-grounded-v3`，保留来源哈希输入和归一化图像身份。真实试点重放维持原 generation，没有因改名强制重新转录或向量化。本轮没有重启用户运行中的服务；加载新模块需要正常重启后端。

## 6. 验证与证据

- [契约比较](../../data/reports/question_practice_refactor/contracts-comparison.json)：49 张表、50 个 Schema 类、47 条展开路由、42 个 OpenAPI 路径、14 份 Skill 对照，差异为 0。
- [真实查询对照](../../data/reports/question_practice_refactor/retrieval-after.json)：6 组真实 MySQL / ES 查询的题号、排序、分数、公开题面与通道集合一致，覆盖精确题号、文本同构、含图同构、同方法、旧材料检索和旧查题。
- [真实 HTTP 与重放](../../data/reports/question_practice_refactor/api-and-replay.json)：进程内 FastAPI 测试调用真实 MySQL / ES；验证新旧入口、公开投影、会话范围、外题资产拒绝、图文通道和源题排除。16 题试点重放新增上游模型请求和投影记录均为 0。这不等于已重启服务后的浏览器验收。
- [依赖审计](../../data/reports/question_practice_refactor/dependency-audit.json)：237 个 Python 模块无层次违规、导入环、缺失导入或未解析的应用导入。
- [新增规则回归](../../backend/tests/test_question_resource_policy.py)：覆盖权限差别、撤回、参考题、SQL 与 Python 一致性、新旧检索正常和故障路径、历史冻结、通道取消与部分失败、材料去重、损坏图片和预算隔离。生成题的检索服务故障测试位于 [test_exercise_reviews.py](../../backend/tests/test_exercise_reviews.py)。
- 完整后端回归 **790 项通过**，耗时 811.20 秒；没有失败、错误或跳过。[回归日志](../../data/reports/question_practice_refactor/regression.log)、[JUnit](../../data/reports/question_practice_refactor/regression.xml)与[综合报告](../../data/reports/question_practice_refactor/acceptance.json)记录最终结果。
- [名称迁移](../../data/reports/question_practice_refactor/moves.json)、[变更前源码清单](../../data/reports/question_practice_refactor/source-before.json)和 `source-before/` 保存本轮前后审计依据。

关键入口行数从源码快照核对：旧普通检索 346 → 238，讲题图片上下文 132 → 86，含图检索编排 309 → 280。抽出的共享组件和边界测试会增加其他文件，不能把入口变短说成总项目代码减少，也没有据此宣称检索质量或运行性能提升。

复跑命令（在项目根目录执行）：

```powershell
$env:PYTHONPATH = "backend"
backend/.venv/Scripts/python.exe -m pytest backend/tests -q
backend/.venv/Scripts/python.exe scripts/maintenance/audit_backend_dependencies.py
backend/.venv/Scripts/python.exe scripts/maintenance/check_backend_contracts.py --compare data/reports/question_practice_refactor/contracts-before.json --manifest data/reports/question_practice_refactor/manifest.json
backend/.venv/Scripts/python.exe scripts/evaluation/check_question_practice_refactor.py --compare data/reports/question_practice_refactor/retrieval-before.json --output data/reports/question_practice_refactor/retrieval-recheck.json
backend/.venv/Scripts/python.exe scripts/evaluation/check_exercise_retrieval_api.py --output data/reports/question_practice_refactor/api-recheck.json
```
