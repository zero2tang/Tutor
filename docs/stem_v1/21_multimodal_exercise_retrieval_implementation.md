# 含图习题检索实现与验证

更新：2026-10-22。设计依据：[20 方案](20_multimodal_exercise_retrieval_plan.md)。

已实现公开题面资产绑定、DeepSeek 图示转录、来源校验与一次有界修复、人工或操作者原图复核、版本化文本与图文索引、按意图检索、RRF、多模态重排和降级回执。真实运行使用 MySQL、Elasticsearch 9.3.2、text-embedding-v4 与 qwen3-vl-embedding。DeepSeek 负责转录和相关性重排，不负责生成图片向量。

目前交付为后端能力与独立试点：16 道现有数学题，13 道含图题逐图核对，3 道纯文字题；固定 12 条查询覆盖原题、同构与同方法三种意图。原题库有 7689 道数学题，本轮没有用 16 题索引替换完整题库。图片上传入口、前端专门检索界面和全量课程启用不在本轮交付中。

## 核心调用链

离线：公开 Question → 受控资产绑定 → 模型转录 → Schema 与来源检查 → 待复核表示 → 原图复核的新版本 → 文本分块向量与图文融合向量 → 来源复查 → 同次 ES 别名切换 → MySQL 发布状态。

在线：服务端会话范围 → 精确题号短路 → 查询图示理解或已复核表示复用 → BM25、文本向量、图文向量 → 各通道逐题查主库与资产版本 → 同题聚合与 RRF → 有界多模态重排 → 返回前再次核对来源 → 公开题面和回执。

题干与选项配图保留字段归属；多图融合输入附图片顺序与选项标记。已复核教材题图通过受控资产接口进入公开题目结构，讲题上下文使用同一个资产校验器读取真实像素，不只传链接。

精确题号成功后不调用读图或任何向量模型。相似题检索排除源题及已见题内容身份。图文候选、文本分块先在各自通道按题目去重，每题每通道只投一票。

| 模块 | 责任 |
|---|---|
| `domains/courses/questions/search` | 资产、来源、不可变表示、复核、检索投影、构建租约、模型与检索回执 |
| `application/question_retrieval/assets.py` | 从公开字段绑定图片；教材图示必须另外确认无解答的裁剪 |
| `application/question_retrieval/vision.py` | 模型转录、来源修复、保留完整题面的分批重排 |
| `application/question_retrieval/build.py` | 按来源与处理配置构建索引，支持缓存恢复和版本发布 |
| `application/question_retrieval/service.py` | 查询范围、并行召回、版本检查、去重、重排与回执 |
| `domains/courses/retrieval/fusion.py` | 按题目聚合的 RRF 规则 |
| `infrastructure/documents/question_images.py` | 本地白名单目录、文件与像素校验、哈希及 Base64 读取 |
| `infrastructure/clients/multimodal_embedding.py` | 已核对的阿里云融合向量契约与独立向量缓存 |
| `infrastructure/clients/question_index.py` | 独立版本索引、成对别名发布和撤回 |
| `api/routes/question_retrieval.py` | 会话内检索及公开题图读取，不接受调用方自报权限 |

## 表示与公开边界

模型转录保存 OCR、公式、明确条件、所求、小问、图片作用与未知项。每个事实必须关联题干原文引用，或当前题图资产及归一化区域。区域属于模型估计或操作者修订，不冒充像素级人工标注。程序校验能阻止无来源条件，不能证明模型已经正确理解图片。

只有 approved 表示中的非 unknown 条件进入增强文本。未复核时使用原生题面；原图完整时仍可构建融合向量。OCR 与原生文字按空白及数学分隔符归一化去重。超长文本保存完整哈希及各段实际输入，分成至多 10000 字符的同题视图，没有静默截断题末内容。

图片只从题干、选项及经检查的公开图片关联读取。解答配图不作为输入。教材整页不能直接作为题图，必须先通过小节原页复核，再由可信本地审阅文件逐图确认题目关联、原页来源和无答案裁剪。裁剪原件位于受控教材资产目录。

方法参考单独保存在 `exercise_search_methods`，只提供可信本地登记，没有公开读取接口，不进入公开题面索引。当前 same_method 使用公开题面及图示相关性重排；内部参考解析辅助排序尚未启用。

公开响应不包含答案、解析、模型原始回执或本地文件位置。新增题图接口会再次核对当前题目、关联与哈希。部署仍沿用本项目的本机单用户会话模型，没有新增多租户身份认证能力。

## 向量与版本治理

文本与图文分别使用独立索引和独立编码器。qwen3-vl-embedding 使用 `contents` 中的真实 Base64 图像和文本，显式设置 `enable_fusion=true`、`dimension=1024`。纯文字查询图文索引时也使用同一个图文编码器。不同模型即使维度相同，也禁止跨空间查询。

来源哈希覆盖题面版本和绑定图片的内容哈希；配置哈希包含表示版本、模型、维度、接口地址及分块策略。图片变化会使旧投影失效。模型请求有独立缓存，用量回执区分真实请求、重试和缓存命中。

构建按课程与命名空间持有有期限的数据库租约。每题写入幂等文档，已成功的向量不重复请求；失败释放租约，进程异常退出后等待租约到期恢复。发布前复查全部来源，并在一次 ES 操作中切换文本和视觉别名。ES 与 MySQL 没有分布式事务；中间失败时版本核对会拒绝过期结果，重跑可恢复发布。

撤回先更新主库可见性，所有新旧检索返回前都检查公开状态；清理索引失败不能让被撤回题目继续返回。既有教材撤回状态 source_incomplete 也会被排除。

内容重复检测优先使用已登记的 canonical_item_id，否则使用规范题面和归一化图像身份。能覆盖相同内容及部分无损缩放；复杂截图、裁剪、旋转和语义近重复仍需来源组治理，不能据此声称完全解决近重复识别。

## 运行方式

在项目根目录设置 Python 路径，使用现有模型和数据库连接配置，不把密钥写入命令。

```powershell
$env:PYTHONPATH = "backend"
backend/.venv/Scripts/python.exe -m app.application.question_retrieval.cli inventory --course math --question-ids math:1014,math:1039 --output data/reports/exercise_retrieval/inventory.json
backend/.venv/Scripts/python.exe -m app.application.question_retrieval.cli extract --course math --question-ids math:1014,math:1039 --output data/reports/exercise_retrieval/extracted.json
```

extract 必须明确题目列表。审阅文件逐条提供 representation_id、source_hash、修订后的完整 payload、reviewer 和 notes，参考本轮 `data/reports/exercise_retrieval/source-review.json`。复核产生新版本，保留原转录和失败响应。

```powershell
backend/.venv/Scripts/python.exe -m app.application.question_retrieval.cli review --course math --input data/reports/exercise_retrieval/source-review.json --output data/reports/exercise_retrieval/reviewed.json
backend/.venv/Scripts/python.exe -m app.application.question_retrieval.cli build --course math --question-ids math:1014,math:1039 --namespace another-pilot --visual --output data/reports/exercise_retrieval/another-build.json
```

命名空间内一次构建发布完整快照；不是向现有课程索引追加两道题。完整课程正式发布使用 main，试点必须使用其他命名空间。不要省略题目列表就直接运行图文全量构建。

在线开关来自配置：

| 配置 | 默认与作用 |
|---|---|
| `TUTOR_EXERCISE_SEARCH_COURSES` | `math,junior_math_7a`，允许接入统一新入口的课程 |
| `TUTOR_EXERCISE_SEARCH_NAMESPACE` | `main`；本轮试点为 `pilot-v1` |
| `TUTOR_EXERCISE_MM_ENABLED` | `false`，图文通道开关 |
| `TUTOR_EXERCISE_RERANK_ENABLED` | `false`，按需启用昂贵重排 |
| `TUTOR_EXERCISE_MM_INTENTS` / `TUTOR_EXERCISE_RERANK_INTENTS` | 默认三种意图，允许逐意图启用 |
| `TUTOR_EXERCISE_CANDIDATES` | `30`，单通道候选上限 |
| `TUTOR_EXERCISE_RERANK_LIMIT` | `6`，完整候选可按上下文预算分批 |

评测入口显式选择 A/B/C/D，不依赖在线开关；公共请求不能指定评测组别或改写开关。现有用户服务没有被本轮重启，加载新路由和配置需要正常重启后端。

新接口 `POST /api/sessions/{session_id}/exercise-search` 接受 query_text、intent、source_question_id、asset_ids、top_k。课程、学生和已见题排除从服务端持久化会话产生。asset_ids 必须关联当前课程的指定公开题目；此契约不代表已有图片上传功能。原 GET 查题入口与 Agent search_exercises 也进入统一检索服务。

## 真实试点与验收证据

- 固定标签：[pilot-v1.json](../../data/evaluation/exercise_retrieval/pilot-v1.json)。全部是开发集，来源变体未跨划分；标签来自 Codex 对原图的核对，尚无独立教师复标。
- 原题与资产清单：[inventory.json](../../data/reports/exercise_retrieval/inventory.json)。16 道题的原文、来源、图片作用及复核状态。
- 构建：[build.json](../../data/reports/exercise_retrieval/build.json)。16 个文本视图与 16 个融合向量，索引模型与维度保存在报告中。
- 原图修订：[source-review.json](../../data/reports/exercise_retrieval/source-review.json)。真实遗漏包括算柱珠数、矩形分隔线、直角标记和染色区域邻接；未把解题答案写入条件。
- 对照：[comparison.json](../../data/reports/exercise_retrieval/comparison.json) 与 [原图逐题报告](../../data/reports/exercise_retrieval/comparison.html)。A 为既有题面文本检索，B 为已复核图示文本，C 增加兼容图文召回，D 再增加多模态重排。
- API 与重放：[api-and-replay.json](../../data/reports/exercise_retrieval/api-and-replay.json)。使用真实 MySQL 与 ES 的进程内 FastAPI 测试，不冒充已重启用户服务的端到端前端验证。
- 自动回归：`backend/tests/test_multimodal_exercise_retrieval.py`，另回归课程范围、题图、模型计量、目录与后端依赖约束。
- 综合验收：[acceptance.json](../../data/reports/exercise_retrieval/acceptance.json)。765 项完整后端回归通过；之后的新增修订完成 78 项关键链路回归，最后的 Agent 范围调整另有 33 项回归通过。索引重放新增上游模型请求与投影记录均为 0。

最终固定开发集的 12 条查询，共 48 次真实检索结果：

| 组别 | Recall@5 | nDCG@5 | 中位耗时 | 降级 |
|---|---:|---:|---:|---:|
| A 原题面文本 | 0.8750 | 0.8540 | 1.02 秒 | 0/12 |
| B 增强图示文本 | 0.9583 | 0.8951 | 0.76 秒 | 0/12 |
| C 图文补充召回 | 0.9583 | 0.9103 | 1.11 秒 | 0/12 |
| D 加多模态重排 | 0.9583 | 0.9338 | 5.11 秒 | 0/12 |

本轮向量缓存已经预热，不是冷启动性能实验。A/B/C 没有新增上游请求；D 实际执行 16 次模型请求，输入 34872 Tokens、输出 5748 Tokens。这里只统计最终这一轮，历史提取、首次构建和失败调试另有回执，不能把缓存测试当作零成本构建。

具体退化：衣物搭配的同构查询在 B/C 中可能排到着色等宽泛计数题之后，未召回全部相关题；图示增强并不保证每条查询都提升。相同查询多次重排也有名次变化。当前样本的平均增益不能证明全题库质量已达标。

评测脚本：

```powershell
backend/.venv/Scripts/python.exe scripts/evaluation/check_multimodal_exercise_retrieval.py --dataset data/evaluation/exercise_retrieval/pilot-v1.json --output data/reports/exercise_retrieval/comparison.json
backend/.venv/Scripts/python.exe scripts/evaluation/check_exercise_retrieval_api.py
```

首次重排存在上下文预算降级，原始失败记录保留在 comparison-before-budget-fix.json；后续采用完整查询和完整候选的分批重排。分批相关性等级没有跨批校准保证，重排结果也不是数学正确性证明。

当前样本太小，不能宣称泛化准确率提升。尚未覆盖物理图示、函数图像、整题截图、跨页、所有同图异条件困难负例，也未完成独立测试集发布门槛。重排增加耗时与模型用量；报告包含退化样本和所有返回排名。费用单价未配置，只报告真实请求与 Tokens，不虚构金额。

接口依据：[阿里云官方多模态向量接口](https://help.aliyun.com/en/model-studio/multimodal-embedding-api-reference)。本轮真实调用已验证融合接口契约；这不代表中文数学相关性已获全面验证。
