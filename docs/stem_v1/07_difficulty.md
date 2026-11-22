# 题目和知识点难度

## 数学口径

所有数值为 **0–100，越大越难**。知识点采用用户确认的算术加权平均：

\[
D(c)=\frac{\sum_{e\in E(c)}n(c,e)d(e)}{\sum_{e\in E(c)}n(c,e)}.
\]

不平方，不取平方根。权重是题目与考点在**原始课程标注**中的关联出现次数，不是学生作答次数。例如两题难度80与20，标注次数3与1，结果为65。数学原始 `knowledge_route_ids` 可能重复同一个叶节点，导入后的 `concept_ids` 已去重，因此必须从原始路线恢复次数；不把目录祖先一起算进来。

未知题不填0。有任一关联题未知时，完整知识点值为null；另存 `partial_value`（已覆盖子集均值）、`scored_weight`、`total_weight` 和覆盖率。部分覆盖点与分类目录不进入完整难度排名；无题点为未知。

## 题目内容估计与论文的区别

DLPR论文第4.2节公式(2)按群体错误率乘预设量级定义题目难度。原论文依赖作答记录。本次不读取或导入历史交互，因此不宣称复现该实测数值方法。采用 `content_rubric/v1` 冷启动内容估计；0–100分数不是答错概率，也不代表个人掌握程度。

模型按固定0–4锚点评估五个维度：抽象程度、推理、知识整合、运算负担、表征转换。程序计算：

\[
d(e)=\frac{25a+30r+20i+15c+10p}{4}.
\]

这组权重和锚点是本项目配置，未经实测校准，**不是DLPR原有量表**。完整量表在 `backend/app/domains/courses/difficulty/rubric.py`。数学参考群体为已有小学基础运算知识但未受专项训练的学习者；力学为已学高中物理和基础微积分、初学工程力学及有限元的大学生。因此仅在同一课程、同一配置内比较，不做跨课程总榜。

离线请求包含公开题干、选项、原始答案与解析及考点名称，不含学生数据。批处理中单独关闭DeepSeek推理模式并留足输出预算，保持对话Agent原有设置；配置保存该选项及提示词哈希。未向模型提供图片像素，有图题的置信标签最多medium；若文字和原解析不能重建任务则保持未知，原始缺图题直接未知。模型自报的置信标签不代表统计置信区间。题目损坏不自动等于更难。

## 数据与代码

| 表 | 用途 |
|---|---|
| `difficulty_profiles` | 课程、方法、模型、版本、参考群体、配置哈希、构建状态、覆盖和用量 |
| `difficulty_scores` | 题目或知识点0–100值、未知/部分覆盖状态、内容哈希、维度及聚合证据 |
| `question_concept_weights` | 原始题目考点关联次数与来源 |

原始 `questions.difficulty` 保留原样；`validation.difficulty_estimate` 是便于现有公开DTO读取的投影，权威口径与聚合值在新增表中。API公开 `difficulty_value` 和 `difficulty_profile`。批量估计理由仅存服务端，避免理由复述答案；模型工具不返回题目标准答案和原始解析。

代码按模型表、量表、批处理、查询服务、工具、API拆分。计算期间不持有数据库事务；每批单独提交。同一内容与配置的已处理项自动复用，失败或内容变化项重算。缺图与其他信息不足项保持未知，补齐数据后内容哈希变化会触发重算。离线计算不提供给Agent作为任意写库工具。

## Agent工具与API

- `get_difficulty_profiles`：查询口径、参考群体、覆盖与构建状态。
- `get_difficulty_details(entity_type, entity_id, profile_id?)`：查看一道题或一个知识点难度。
- `search_by_difficulty(entity_type?, min_difficulty?, max_difficulty?, order?, concept_ids?, limit?, offset?, profile_id?, query?)`：按数值排序和区间筛选，不经过RAG。习题考点筛选为任一匹配。
- 原 `rank_learning_targets(criterion="difficulty")` 使用已存知识点加权值，保留图谱层级与个人薄弱项各自指标。

前三个角色可使用上述三个只读工具；评估角色可读口径与难度详情。全部工具强制沿用当前会话课程；不同配置不自动混排，未知不作为零分。

HTTP：`GET /api/difficulty/profiles`、`GET /api/difficulty/detail`、`GET /api/difficulty/search`。统一使用 `course_id`；detail需要 `entity_type` 与 `entity_id`，search支持区间、升降序与分页。习题公开搜索及练习卡片会显示“估计难度 x/100”。学生历史消息中的冻结题目契约不会为此次计算而重写。

```powershell
# 初次计算/断点继续。只处理已导入的原始课程题。
backend\.venv\Scripts\python.exe scripts\data\build_difficulty.py --course all
# 用独立SQL复核加权均值，并让真实主Agent做数值排名查询。
backend\.venv\Scripts\python.exe scripts\evaluation\check_difficulty.py
```

构建结果保存在 `data/reports/mechanics_difficulty.json`、`math_difficulty.json`；独立验收在 `difficulty_acceptance.json`。分数用于第一版内容排序，仍需后续人工抽检和群体数据校准。

## 本次计算结果

| 课程 | 原始题目 | 有分数 | 信息不足保持未知 | 完整评分知识点 | 部分覆盖知识点 | 未知知识点 |
|---|---:|---:|---:|---:|---:|---:|
| 力学 | 177 | 174 | 3 | 192 | 5 | 1 |
| 数学 | 7,652 | 7,604 | 48 | 840 | 24 | 1 |

数学另有310个分类目录，不参加考点难度排名。两门课程均无剩余失败或待处理题。难度值采用DOUBLE保存；独立SQL按原始权重重新计算完整均值，并逐一核验未知值和部分覆盖状态。
