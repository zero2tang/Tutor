# 教材多模态提取：定义与教学模式、习题、方法

本流程使用当前配置的多模态 DeepSeek，页面图像与原生文字一起输入；没有另接OCR服务。以小节为单位保存三类资源，MySQL为事实来源，暂不向量化。

## 调用与存储

`application/textbooks/cli.py` → 原件与目录准备 → 页转录 → 独立页审校 → 小节教学归纳 → 来源验证 → 数据库原子提交。

- `infrastructure/documents/pdf.py`：整书SHA256、原生文字和行坐标、Poppler页面与图片区域缓存。PDFium对本教材存在字体/图片兼容问题，不能作为本轮渲染器。
- `application/textbooks/`：模型提示、真实调用、审校、格式修复、组装与范围门槛。
- `domains/materials/extraction/`：输出契约、任务租约、回执、版本、三类资源和复核后目录投影。
- 新增五张表：`textbook_extraction_books`、`textbook_extraction_sections`、`textbook_extraction_jobs`、`textbook_extraction_calls`、`textbook_extracted_resources`。

离线任务使用同一主库，先增加新表，不修改旧教材档案。原件保存完整字节与SHA256；模型转录与归纳保留来源页、图片哈希和估计区域。定义和习题为来源转录，教学设计和归纳方法明确标记为模型归纳；没有原文答案的习题保持null。

## 分阶段运行

从`backend/`执行：

```powershell
.\.venv\Scripts\python.exe -m app.application.textbooks.cli --source 'D:\Donwload\义务教育教科书·数学七年级上册.pdf' --prepare-only
.\.venv\Scripts\python.exe -m app.application.textbooks.cli --source 'D:\Donwload\义务教育教科书·数学七年级上册.pdf' --section 1.1
```

原页复核JSON包含`payload_hash`、`reviewer`、`passed`和检查范围，通过`--section 1.1 --review <回执路径>`登记；复核修订快照时再提供`section_id`。若有局部更正，`--amend <更正JSON>`使用`source_section_id`、`reviewer`和`patches`创建新快照；每个patch含`path`、`before`、`value`和`source_refs`。原始模型输出不改写。第一章门槛认可同一流水线版本的已复核修订版。

仅在1.1的内容指纹和原页复核匹配后运行：

```powershell
.\.venv\Scripts\python.exe -m app.application.textbooks.cli --source 'D:\Donwload\义务教育教科书·数学七年级上册.pdf' --chapter 1
```

命令当前只允许第一章。该教材第一章范围为PDF第8—59页，书内第1—52页。小结与复习题在同页交接，目录保存纵坐标边界。数学活动、阅读拓展等保留原栏目，不伪造为新的基础概念。

## 恢复与发布

同一模型、提示、页面图像和输出预算的成功调用复用已验证回执；失败响应、Tokens、耗时和网络请求回执均保留。结构输出错误最多追加一次格式修复，不放宽数学来源要求。任务租约在领取和提交时检查，模型请求期间不持有写事务。全部分页任务收束后再报告失败，重新执行命令可恢复，不需重跑已完成请求。

原始提取结果进入专用资源表，状态为`extracted`，尚不发布到通用题目/材料目录。原页复核通过后才投影；复核失败保留档案并撤回已有公开投影。章节其他单元完成模型审校不能冒充逐页人工复核或教师审校。

## 查询与验证

- `GET /api/textbook-extraction/books?course_id=junior_math_7a`
- `GET /api/textbook-extraction/books/{book_id}/sections`
- `GET /api/textbook-extraction/sections/{section_id}`：公开结构不返回参考答案、解答或原始分页回执。
- `GET /api/textbook-extraction/jobs/{job_id}`：任务状态和真实用量。

从项目根目录运行`backend/.venv/Scripts/python.exe scripts/evaluation/check_textbook_extraction.py --book-id <教材ID>`生成数据库一致性报告及原页对照HTML。报告中的`complete`仅代表范围、来源和存储检查；不是教材提取正确率、教师认可或学生学习增益。

真实试点发现：题目数正确仍可能遗漏数列；模型审校也可能误改正负号、表格方向和图题。故保留原图、初始输出、审校输出及原页更正，不能将同模型二次检查作为数学真值。

## 本轮真实结果

第一章9个目录单元、52个源页已存入MySQL，共40条定义/性质、150条顶层题目记录、51条方法记录、33个图示记录。方法数量是记录数，包含不同来源和模型归纳，不代表51种独立技巧。

1.1经过逐页复核并保存独立更正快照；其他8个单元保持待复核状态。章节抽查修正了天气图符号、图片区域、跨页例题解答及栏目归属，但不声称全章逐字无误。五张新增表保留原件、结果、任务及调用记录；当前选定版本的三类资源与教学设计共250行。

真实模型调用累计129次，其中7次输出结构或截断失败；全部原始失败回执保留，阶段恢复未丢弃已完成调用。累计输入443,738、输出146,782 Tokens，模型调用耗时之和570,915.72 ms（含并发，不是端到端基准）。8次调用回执跨版本复用；整章再次执行验证为零新增模型调用、零新增回执行。

后端完整回归740项通过，后续最终相关回归34项通过，缓存映射修正后新增流程14项通过。4个FastAPI只读接口使用TestClient实际读取MySQL验证，包含课程隔离和答案隐藏；没有重启用户现有服务。验收、API、恢复和原页对照文件位于`data/reports/textbook_extraction/`。
