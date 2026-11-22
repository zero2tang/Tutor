# 物理与数学数据库扩展

更新日期：2026-10-19。本轮物理沿用已接入的力学课程，新增 XES3G5M 数学课程，完成事实库、ES、Neo4j、课程隔离及网页课程切换。本文记录当前实现；[04 文档](04_implementation_and_acceptance.md) 保留较早力学 V1 快照。

## 数据来源与保留范围

| 课程 | 来源 | MySQL 原题 | 知识节点 | 公开 RAG 材料 | 当前可开放独立练习 |
|---|---|---:|---:|---:|---:|
| 力学 | 本地 CBEGRec 的 `Mechanical_Physics`，上游 MOOCRadar `C_697360` | 177 | 198 | 219 | 174 |
| 数学 | 官方 XES3G5M 教学元数据 | 7,652 | 1,175 | 1,175 | 7,562 |

本表只统计原始课程题目。系统另行保存、标注生成变式，ES 的全索引题目数可能因此大于两门课程原题之和。知识节点数量也不等于可独立评估的细粒度知识点数量。

数学保留文件位于 `data/raw/xes3g5m/metadata/`，包含 `questions.json`、`kc_routes_map.json` 和 3,869 个图片文件，共 3,871 个文件。保留文件的本地 SHA256 全部核对通过；摘要是本地完整性记录，不是官方签名。教学元数据子树已经取得完整，临时压缩归档仅为下载前缀；当前用途不需要继续下载交互部分。

两门课程均未导入原始学生档案、历史答题序列或训练测试交互。当前用户在系统内产生的真实目标、对话、作答和学习证据继续保存在独立业务库中。

官方入口为 [XES3G5M 仓库与元数据说明](https://github.com/ai4ed/XES3G5M#metadata)、[7,652 道有效题的官方澄清](https://github.com/ai4ed/XES3G5M#clarification)、[MOOCRadar 仓库](https://github.com/THU-KEG/MOOC-Radar)。数学界面使用中性名称“**小学数学与拓展思维**”，不根据数据集缩写推断年级。

## 知识节点、图谱与 RAG 口径

数学保留全部 1,175 个原始节点 ID：865 个在题目路径中作为叶节点使用，另有 310 个分类节点。原始不同叶名称为 865，去除首尾空格后为 863；全部节点原名为 1,175，去除首尾空格后为 1,172。差异来自“ 几何思想平移”“分数化为小数 ”和“ 小数基础”等空格变体，导入没有据此合并不同原始 ID。

| 投影 | 力学 | 数学 |
|---|---|---|
| ES 课程原题文档 | 177 | 7,652 |
| ES 公开材料文档 | 219 | 1,175 |
| Neo4j 概念节点 | 198 | 1,175 |
| Neo4j 原题节点 | 177 | 7,652 |
| 概念之间的分类关系 | 本轮未新增 | 1,314 条 `HAS_CHILD` |
| 概念之间的同题关联 | 281 条 `RELATED_TO` | 764 条 `RELATED_TO` |

Neo4j 物理关系名使用项目命名空间，如 `STEM_HAS_CHILD`、`STEM_RELATED_TO`。分类关系说明上位与下位知识节点，同题关联说明多个概念在同一道题中共同出现；两者都不等于经过教学验证的先修条件。表中概念关系数量不包含题目与概念之间的考查关系。

MySQL 是事实来源；ES 使用共享的 `stem_tutor_questions_v1` 与 `stem_tutor_materials_v1`，通过 `course_id` 过滤。Neo4j 同样保存课程标识，接口拒绝跨课程概念和未导入课程。投影通过 `MERGE` 增量写入，可以补齐或更新已有记录，但**不会删除旧图边**；若来源关系被移除，不能把再次执行投影当作已清除旧边的证据。

向量仍由已配置的 DashScope `text-embedding-v4` 生成，ES 保存 1,024 维向量并执行关键词与向量检索。公开投影包含题干、选项、知识点和来源，不包含标准答案、私有解析或学生状态。

数学目前的 1,175 份 RAG 材料是知识分类、原始名称和路径元数据，**不是完整教材**。原题解析保存在后端供作答后使用，不会作为可在待答期间自由检索的公开材料。力学 219 份材料为 198 份概念上下文和 21 份补充教学卡片，也不等于 219 篇原始教材。

## 数据质量、配图与多空题

| 隔离原因 | 数学题数 | 处理 |
|---|---:|---|
| 选项图重复引用同一占位符 | 42 | 保留原始数据，不猜测图像与选项的正确映射 |
| 仅一个“选我即可查看答案”选项 | 18 | 作为原平台交互占位题隔离 |
| 重复错误干扰项 | 5 | 属于选项质量问题，不描述为正确答案键歧义 |
| 图示或原始版式待人工核对 | 25 | 沿用缺图防护策略，暂不开放独立练习 |
| **合计** | **90** | 仍可检索并查看中文原因，练习接口提前拒绝 |

因此数学公开接口的最终开放范围为 \(7652-65-25=7562\) 道。`math_import.json` 记录导入器识别的 65 道来源异常及 7,587 道候选题；`course_database_acceptance.json` 另行核对公开接口的 25 道版式待审题与最终 7,562 道，不能直接把导入报告里的候选数当成交付可练数。力学另有 3 道缺图题，开放 174 道原题。

全部配图共有 3,869 次文本引用、3,767 个唯一引用，已识别引用没有缺失文件；102 个图片文件未被当前文本引用。图库同时包含题干、选项和解析配图。待答题公开接口只返回题干与选项所需图片，解析图不在待答阶段暴露。网页使用 `/api/assets/math/` 本地资源显示图片，不执行原始 HTML。

图片文件可读取不表示图意正确，也不表示系统已经理解图片。当前模型是文本模型，不能看图；遇到必须依赖图示的推理时，不能把文字生成的解释当作已验证解答。25 道版式隔离与“所有已识别图片引用均存在”并不矛盾：前者针对原题图示或排版信息不足，后者只核对已有图片占位符与文件。

数学原题包含 6,142 道填空和 1,510 道单选。填空中 4,716 道为单空，1,426 道为有序多空，共 3,601 个多空答案位置。多空答案按原始顺序保存并按空位比较，不能把列表中的多个答案解释成同一空位的等价答案。完整题号如 `math:7644` 优先精确匹配，也可使用当前课程内原始数字题号。数学等价表达式的全部情况尚未经过系统校验，无法精确匹配时仍依赖受置信度约束的语义诊断。

## 网页与四个 Agent

侧边课程选择支持力学与数学；切换时刷新概念、题库、学习档案、历史和图谱，并清空当前工作台。恢复已保存会话以服务端 `course_id` 为准，旧版无该字段的会话按力学处理。课程数据请求带版本检查，慢返回的上一门课不能覆盖新课程页面。

数学学习目标可自由输入，快捷入口来自真实有题的知识叶节点。原有四个业务角色继续复用 Python 运行时、角色 Skills 与工具权限：主 Agent 决定下一步，知识点 Agent 讲解，习题 Agent 选择或生成练习，评估 Agent 预测、诊断与反思。图谱图例区分分类关系与同题关联。

判分和掌握估计是不同结果。即使答案客观正确，辅助作答、重复题目或诊断置信度不足时，服务也可以保存回答而不增加独立掌握证据。页面显示的掌握百分比仍是尚未校准的诊断估计。

## 本轮验收与证据

| 验收 | 结果 | 范围与限制 |
|---|---|---|
| 数据库与接口 | 14/14 | 课程计数、ES 公开字段、Neo4j 关系、课程隔离、题号、配图、多空结构、异常题前置拒绝；混合检索复用已有向量缓存，不发出新的模型请求 |
| 数学真实四 Agent 流程 | 4/4 | 真实教学、单空预测与诊断、多空按序判分、幂等及四角色课程范围；使用独立验收档案 |
| 力学多课程回归 | 19/20（95%） | 原固定任务完整重跑；T14 客观判分正确，但诊断置信度 0.6 未达到 0.8，未更新掌握估计 |
| 后端单元测试与前端构建 | 23/23；构建通过 | 单元约束检查，以及 TypeScript 与 Vite 生产构建 |
| 浏览器实测 | 通过 | 两课程切换、`math:20` 精确检索和原图显示、`math:279` 异常提示及禁练 |

数学单空流程也出现判分正确但 `state_update_count=0` 的情况；4/4 说明链路符合实际约束，不表示每次正确回答都会提升掌握估计。力学 T14 仍按未通过保留，没有为了提高成功率强行改变置信度。三组任务范围不同，不能相加后作为模型准确率。

- [本轮构建与验收汇总](../../data/reports/physics_math_build_summary.json)
- [数据库与接口逐项验收](../../data/reports/course_database_acceptance.json)
- [数学真实四角色流程](../../data/reports/math_learning_smoke.json)
- [本轮力学回归快照](../../data/reports/mechanics_multicourse_regression.json)
- [数学事实导入报告](../../data/reports/math_import.json)
- [数学 ES 与 Neo4j 投影报告](../../data/reports/math_indexing.json)
- [原始教学文件与图片审计](../../data/reports/xes_source_verification.json)

这些检查提供功能路径和数据库一致性证据，尚未评估整个课程的教学质量、全部图题的正确性、检索召回率、学生学习增益或长期状态校准。

![数学课程、题号检索与原始配图](../../data/reports/screenshots/math-course-database.png)

## 复现命令

以下均从项目根目录执行，依赖配置与启动方法见 [项目说明](../../README.md)。已有教学文件无需重复下载。

```powershell
# 只读取教学文件并更新本地审计报告。
backend\.venv\Scripts\python.exe scripts\data\audit_xes_metadata.py

# 重新导入事实并同步课程投影；按缓存需要请求已配置的向量服务。
Push-Location backend
.\.venv\Scripts\python.exe -m app.application.commands.courses.import_course --course mechanics 'D:\Python项目\CBEGRec-main\data\Mechanical_Physics'
.\.venv\Scripts\python.exe -m app.application.commands.courses.import_course --course math '..\data\raw\xes3g5m\metadata'
Pop-Location

# 仅重新投影数学课程，或仅导入事实时，分别使用以下参数。
Push-Location backend
.\.venv\Scripts\python.exe -m app.application.commands.courses.import_course --course math --index-only
# .\.venv\Scripts\python.exe -m app.application.commands.courses.import_course --course math --import-only
Pop-Location

# 应用服务启动后运行数据库检查；后两项会实际请求模型。
backend\.venv\Scripts\python.exe scripts\evaluation\check_course_database.py
backend\.venv\Scripts\python.exe scripts\evaluation\check_math_learning.py
backend\.venv\Scripts\python.exe scripts\evaluation\run_mechanics.py

# 单元测试在项目可写目录创建临时文件，不依赖本机系统 Temp。
backend\.venv\Scripts\python.exe -m pytest backend\tests -q -c backend\pyproject.toml --basetemp=data/tmp/pytest-course-database
Push-Location frontend
npm run build
Pop-Location
```

构建只针对指定课程，不清除用户学习记录；Neo4j 增量投影仍保留前述旧边限制。验收脚本使用独立 `evaluation_*` 档案，报告写入 `data/reports/` 对应文件；复现前可先备份现有报告，以保留本轮证据。
