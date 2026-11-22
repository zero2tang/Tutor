# 主 Agent 自主决策与按需 Skill

本次调整优先解决自然语言请求被过早绑定到知识点、强制进入教学和固定两轮工具调用的问题。Python 后端保持四个业务 Agent，借鉴 Pi 的可扩展工具循环和 Skill 按需加载理念，没有迁移到 Pi 的 TypeScript SDK。

参考：

- [Pi Agent Core 的工具循环](https://github.com/earendil-works/pi/blob/main/packages/agent/README.md)
- [Pi Skill 的渐进加载](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/skills.md)

## 当前执行流程

1. 创建会话时只绑定用户明确选择的知识点 ID。自然语言目标直接提供给主 Agent，不先执行名称子串匹配或 RAG。
2. 主 Agent 获得课程身份、当前工作区、用户操作偏好、近期对话和待澄清决策。旧知识点是上下文，不限制新目标。
3. 模型自主决定直接回应、澄清、读取 Skill、浏览课程、查询学生状态或安排专业教学。
4. 工具结果、参数错误和可执行性校验反馈返回给模型，允许它补查并修正。
5. 最终结构化动作交给业务服务执行。课程身份、题目契约、独立作答和掌握证据写入仍由服务端校验。

这里没有按“最难”等词汇编写规则路由。提示和专业 Skill 说明如何考虑歧义，实际工具选择与执行顺序由模型决定。

## Runtime

| 文件 | 职责 |
|---|---|
| `backend/app/agent/runtime/runner.py` | 角色调用入口、运行记录、事件、用量与整体超时 |
| `backend/app/agent/runtime/loop.py` | 模型与工具往返、错误恢复、结构化输出修复、重复 Skill 读取缓存 |
| `backend/app/application/agent/prompts.py` | 简短角色职责、能力说明和输出协议 |
| `backend/app/agent/runtime/skills.py` | 技能用途目录、角色范围内的正文读取 |
| `backend/app/agent/runtime/payloads.py` | 工具结果压缩，保留合法 JSON 和局部覆盖标记 |
| `backend/app/application/learning/planning.py` | 提供会话上下文、校验可执行性、保存目标与澄清状态 |

主 Agent 默认 `skill=auto`，只获得 Skill 名称和用途摘要，按需调用 `read_skill`。知识点 Agent 也改为按需选取基础、进阶或熟练层教学方法。习题与评估的固定专业子任务仍保留服务指定的 Skill，同时具备按需读取本角色其他 Skill 的能力。

当前预算：主 Agent 最多 8 个开放工具的模型轮次，其他角色为 6 轮；最多 16 次工具请求；额外 2 轮用于最终回答或协议修复；单次角色调用总超时 240 秒。预算是运行边界，不要求用完。普通回应和澄清不要求发生工具调用。

同轮多个工具调用已支持有界并发，默认每个 Agent 最多同时执行 4 个工具，结果按请求顺序回填。同步数据库工具在线程内各用独立连接；需要前置结果的查询继续分轮。详见 [同轮工具并发执行](10_tool_concurrency.md)。

## 工具能力

| 工具 | 作用 |
|---|---|
| `read_skill` | 读取本角色已发现的专业方法，拒绝任意路径与跨角色加载 |
| `get_course_overview` | 返回课程总量、具体概念与目录数量、图关系种类、难度数据配置与覆盖 |
| `search_concepts` | 精确或关键词查找、模块筛选、空查询分页浏览；默认排除分类目录 |
| `rank_learning_targets` | 分别按数值难度、可靠先修链层级、分类深度或个人已测薄弱项排序 |
| `get_knowledge_structure` | 支持按关系类型筛选图扩展 |
| `read_student_state` | 明确查询当前目标或整门课程，返回已测与未测数量及局部范围 |
| `search_exercises` | 支持关键词或混合检索、题型、任一或全部知识点覆盖 |

所有工具均继承服务端会话的课程身份；模型不能通过参数切换课程。查询结果给出总量、返回数量或覆盖范围，不能把一页结果解释为整门课程。

数值难度排序只接受有明确 `value`、`scale_max=100`、`higher_is_harder=true`、`method`、`reference_group`、`version` 的 `validation.difficulty_estimate`。不同方法、群体和版本不混排。缺少数据返回 `available=false`，不使用文本相关性、旧认知标签或未测状态代替难度。本次没有批量生成或标定真实题库的难度分。

图谱排序仅使用对应关系：先修层级要求已验证的 `PREREQUISITE_OF`，分类深度使用 `HAS_CHILD`。计算最长前置链层级，遇到环则说明不可用。同题关联不能代替先修关系。

## 澄清与目标切换

`Plan` 新增 `respond`、`clarification` 和 `goal_update`。动作说明明确区分：`teach` 包括概念解释和原理推导，`explain` 专指已经存在的习题题解。没有习题时，校验反馈会交回模型修正，而不是在执行末尾报“没有可讲解的题目”。

待澄清任务保存在会话 `pending_context.decision` 中，包括原始目标、问题、选项和缺失信息。刷新或下一轮短回答均可接续，不依赖进程内临时变量。完成澄清后清除此状态，不写入学生掌握记录。

前端显示可点击的选项，并把选项文本作为下一轮自然语言消息发送。历史选项不能再次点击，用户也可输入自定义答案。首页不再固定发送 `intent=teach`；工具栏中的显式操作仍作为重要偏好提供给模型。

## 验证与实际限制

- 当前版本完成 38 项本地测试、前端 TypeScript 检查与 Vite 生产构建；真实模型 v4 的 7 个场景全部通过，其中 5 个使用公开数学元数据，2 个使用明确标记的人工双指标夹具。
- 已在空闲时仅重新加载后端，并通过实际 HTTP API 验证自然语言目标不预绑定、主 Agent 澄清、待决策状态持久化、消息携带可点击选项、会话进入待澄清阶段、未产生学习证据。API 记录见 `data/reports/agent_autonomy_api.json`，使用专用 evaluation 身份。
- 本地测试覆盖自主结束、零工具澄清、超过两轮工具往返、工具错误恢复、预算、Skill 缓存及参数校验、课程隔离、根目录误绑定、目标切换与概念解释误路由。
- 真实模型脚本：`scripts/evaluation/check_agent_autonomy.py`。公开数学资料加载至独立内存 SQLite，不读写个人档案；另有明确标记的人工双指标课程，用于验证难度排序与先修顺序不同的情况，人工分值不冒充真实标定。
- 机器结果：`data/reports/agent_autonomy_live.json`，包含每个场景的动作、正文、工具轨迹、用量和源码指纹。
- 历史较严格的选项措辞断言保存在 `data/reports/history/agent_autonomy_strict_options_v2.json` 与 `agent_autonomy_strict_options_v3.json`。最终检查区分真实课程数据不足和双指标均可用的受控情况，不混合不同验收版本计算通过率。

模型仍可能进行多余工具调用，或在相同意图下采用不同的澄清表述。测试验证可观察行为与事实边界，不意味着所有自然语言请求均能正确理解。真实课程的难度估计、先修关系建设和更大规模对话评测属于后续工作。

只重新加载后端时，可在确认没有正在执行的学习任务后运行：

```powershell
.\scripts\stop.ps1 -Kind backend
.\scripts\start.ps1 -NoBrowser -BackendOnly
```

这些参数不会停止现有前端。已有会话与学习记录仍从数据库恢复。
