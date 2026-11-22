# 教材结构化解析与教学分析包

> 历史实现记录：当前保留教材规则解析与原文定位，但已移除数学求解、步骤核验和题型适配器。新导入仅保存原文答案与解析，旧分析包作为档案读取；本页旧计算源码和验收命令不再执行。当前流程参见[项目说明](../../README.md)，清理验收使用 `scripts/evaluation/check_tutoring_cleanup.py`，教材界面回归已改写为通用讲题入口。

更新日期：2026-10-21。

## 实际范围与来源

保留现有 FastAPI、MySQL、AgentRunner、工具权限、SSE 与 React。
新增格式解析器和题型分析适配器，先完成一元一次方程章节的结构化来源与教学分析包，
并将教材题接入原有分步辅导。

真实材料为 OpenStax《Elementary Algebra 2e》第二章 2.3、2.4 节的精选原文，
覆盖两边未知数、常数、括号、分数与小数。它不是中国教材的完整章节。
选段有 12 个原文例题、188 个结构块、66 个结构化公式，均有原章节定位。
下载清单保存官方 URL、作者、版次、许可、原件和选段 SHA-256。
当前源许可为 CC BY-NC-SA 4.0，以下载清单和官方原文为准。

12 题均生成限定数学范围内可用的教学包。部分源解答只有图片，辅助描述可能出错；
未把这些描述当作数学真值。缺少原生解答时，步骤来自精确规则推导，标为 `derived_by_rule`。
原生步骤与答案单独核验；矛盾或不支持的输入降为草稿。

## 页面入口

选择“初中数学 · 一元一次方程”，进入“教材与方法”。
可查看题干、各条解法的基础、可选技巧、核验范围和原文位置。
点击“用这道教材题分步练习”，沿用步骤输入、分级提示、辅助标记和新题验证。
原文使用 y、a、m 时，页面说明本次统一用 x 提交，原式及变量映射仍保留。

## 模块与扩展

| 模块 | 职责 |
|---|---|
| `app/domains/materials/parsing/parser.py` | 统一结构、稳定标识、章节与题解组装、格式注册 |
| `app/domains/materials/parsing/html_parser.py`、`mathml.py` | 原生 HTML/MathML、嵌套题解和多段题干、公式结构 |
| `app/domains/materials/parsing/pdf_parser.py` | 原生 PDF 词坐标、分栏、页眉页脚和可疑二维公式 |
| `app/teaching_analysis/registry.py` | 题型适配器注册，不对未知题型猜测 |
| `app/teaching_analysis/linear.py` | 精确方程核验、多条解法、能力要求、提示与补问 |
| `app/teaching_analysis/builder.py` | 包版本、来源引用、关系类型、哈希与发布检查 |
| `app/domains/materials/textbooks.py` | 幂等导入、不可变原件和解析快照、目录投影 |
| `app/domains/materials/teaching_packs.py` | 内部契约、公开投影、课程归属与读取时再核验 |
| `app/tutoring/identity.py` | 跨题号和教材版本的方程内容去重 |

新增文件格式实现格式适配器；新增题型实现教学分析适配器与对应核验器。
当前未覆盖几何证明、应用题建模、参数讨论或任意自然语言步骤。

## 规则解析与兜底

- HTML 使用原生结构、`data-type`、标题、编号、题干/解答边界；脚本和代码块不作为教材指令执行。
- Markdown 使用标题、例/解标记、独立等式和数学定界符。
- MathML 保留分子分母与运算结构；未知元素保留原文并进入疑难区域。
- PDF 使用 pdfplumber 的词、字号及坐标，处理双栏和重复页眉页脚。
- 可疑分式、混合字号、扫描页和图像不强制扁平化为可信公式。
- 每块保留来源 URI、DOM/行号或页码/坐标，嵌套图片、异常和疑难区域也继承各自原章节来源。

试点产生 107 个待复核区域，OCR、视觉模型调用均为 0。
当前兜底区域保存 `pending`、`attempted=False` 与建议处理方式；本轮没有实现自动 OCR/视觉转写，
没有声称图像内容已被识别。进入队列不使其成为数学依据；后续可接入预算受控的局部识别和复核。

## 教学分析包与发布边界

每题保存原题、变量规范映射、解法路径、逐步规则、各路径的基础与技巧、教材出处、
程序推导标记、原生解答核验、诊断问题、提示与完整性哈希。
整体处理和去分母属于可选路径；不把一种技巧列为所有解法必需。
相关题区分 `exact`、`isomorphic`、`method`、`diagnostic`，附理由，
不由同章、标签或相似度自动生成先修关系。
原式已经是孤立未知数时，只供解释复核，不进入独立分步练习。

原件、分析包和已有会话保持不可变，解析或分析版本升级生成新快照。
教材目录默认显示同来源最新解析版本，旧版仍可按 ID 恢复。
新题验证按题号及精确方程内容指纹排除历史原题，防止换题号、展开括号或换版后增加独立证据。
这仍不能证明远距离迁移或长期掌握。

`get_teaching_analysis` 向知识/习题 Agent 提供公开要求、技巧与来源，不返回答案。
教材练习把包 ID、哈希和公开要求冻结到既有教学契约；模型选卡输入携带该包，
回执记录实际消费的 ID/hash，公式继续经过数学发布校验。
公开包接口不返回完整步骤、答案、提示和诊断预期值。
原文块阅读只开放独立定义、规则、方法和标题，排除题干/解答私有块。
公开 Material 使用相同边界；本次没有将教材答案自动投影到 ES 公开索引。

## 接口与存储

- `POST /api/textbooks/import`：HTML/Markdown 文本或 PDF Base64，不接受服务端文件路径。
- `GET /api/textbooks`、`GET /api/textbooks/{id}`：来源、章节、块位置、解析统计和疑难区域。
- `GET /api/textbooks/{id}/blocks/{block_id}`：受公开边界约束的正文。
- `GET /api/teaching-packs`、`GET /api/teaching-packs/{id}`：公开教学分析。
- 原 `/api/sessions/{id}/practice`、`/tutoring`：教材题分步教学及 SSE。

单次导入最多 8 MiB，同标识同内容重放、冲突内容拒绝。
解析在写事务外进行，原件、解析、教学包、目录投影及回执在同一事务提交。

## 复现与验证

`data/raw` 沿用项目忽略规则；仓库中的脚本可下载并校验真实教材：

```powershell
.\backend\.venv\Scripts\python.exe scripts\data\fetch_linear_chapter.py
.\scripts\start.ps1 -NoBrowser
.\backend\.venv\Scripts\python.exe scripts\evaluation\check_textbook_pipeline.py
```

已有原件时可用 `fetch_linear_chapter.py --offline` 校验哈希并确定性重建选段。
验收经过真实 HTTP、MySQL、配置模型和 SSE，学生回答明确为模拟。
报告 `data/reports/textbook_pipeline_acceptance.json` 保存源码哈希、教材来源、逐项断言、
真实模型用量、包消费回执及原件/包/会话的数据库核对。

2026-10-21 最终验证：后端 348 项回归通过，真实后端 9/9 通过，浏览器 4 项通过，
前端构建和原有流式恢复 15 项测试通过。
后端验收实际调用已配置模型 1 次，输入 4,200 Tokens、输出 65 Tokens、耗时 1,248.84 ms，失败 0。
该耗时仅为本次运行记录，不是时延或吞吐基准。

浏览器脚本 `scripts/evaluation/check_textbook_ui.cjs` 使用已有 Playwright 与 Edge，
通过 `TUTOR_BROWSER_MODULES` 指定 Node 包目录，使用独立 `evaluation_*` 档案。
结果与截图位于 `data/reports/textbook_*`。

未验证真实学生学习效果、教师一致性、中国教材版式覆盖率和扫描图片识别质量。
