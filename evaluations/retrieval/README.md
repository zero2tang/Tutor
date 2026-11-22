# 习题检索评测

独立比较四组召回，不调用业务检索入口，不生成答案，不使用图谱、模型重排、题号直达或自动降级。生产 API、数据库表和线上索引别名均不修改。

**当前默认：100 道候选题、24 条查询，开发/测试各 12 条。** 这是按用户降低远程模型用量的要求，从原 1,200 / 240 方案缩减的小规模诊断集。原 `math_medium_v1` 仅有本地快照和一次失败的转录联调，已停止处理。100 道候选题均需要文本编码和图文编码；E2 在线召回不调用编码器。四组复用编码缓存，不重复建立四份向量。查询与相关性标注仍有额外用量。

| 实验目录 | 通道 | 排名 |
|---|---|---|
| `experiments/01_semantic_embedding` | 文本向量 | 相似度 |
| `experiments/02_bm25` | BM25 | BM25 分数 |
| `experiments/03_hybrid_rrf` | BM25、文本向量 | 等权 RRF |
| `experiments/04_hybrid_multimodal` | BM25、文本向量、图文融合向量 | 等权 RRF |

每通道前 50 道，分块按题目聚合去重，RRF 常数 60，正式评价前 20 名。E4 的总召回资源更多，而且纯文字题也进入图文编码器，增益不能全部归因于图片。模型配置来自当前后端配置并写入 `models.json`；固定 `text-embedding-v4` / `qwen3-vl-embedding`、1024 维，两个向量空间完全独立。

## 运行

本次已完成100题/24查询的冻结与四组真实运行，结果见 [RESULTS.md](RESULTS.md) 和 [HTML报告](reports/math_100_q24_v1_initial.html)。默认数据集已经冻结，不能再次修改。查看结果可直接运行 `report`；重放可指定新 `--run-id`。下方完整制备流程用于新版本，须为每个阶段统一传入新的 `--dataset` 路径。

在 `D:\Projects\tutor` 中使用后端 Python 运行主流程。默认数据目录为 `evaluations/retrieval/datasets/math_100_q24_v1`。本地 XES3G5M 元数据只读导出，不连接 MySQL；模型与 ES 使用后端既有配置，密钥不写入产物。

```powershell
backend/.venv/Scripts/python.exe -m evaluations.retrieval prepare --snapshot-only
backend/.venv/Scripts/python.exe -m evaluations.retrieval prepare --limit 3
backend/.venv/Scripts/python.exe -m evaluations.retrieval prepare
backend/.venv/Scripts/python.exe -m evaluations.retrieval index --limit 3
backend/.venv/Scripts/python.exe -m evaluations.retrieval index
backend/.venv/Scripts/python.exe -m evaluations.retrieval pool
backend/.venv/Scripts/python.exe -m evaluations.retrieval label
backend/.venv/Scripts/python.exe -m evaluations.retrieval status
backend/.venv/Scripts/python.exe -m evaluations.retrieval review
backend/.venv/Scripts/python.exe -m evaluations.retrieval verify
backend/.venv/Scripts/python.exe -m evaluations.retrieval freeze
backend/.venv/Scripts/python.exe -m evaluations.retrieval run --run-id math_100_q24_v1_initial
backend/.venv/Scripts/python.exe -m evaluations.retrieval report --run-id math_100_q24_v1_initial
```

`prepare` 完成转录后才生成查询；转录按两张图片组批，结构和来源校验失败则中止并保留进度。仅对能唯一对应原文的空白、LaTeX 美元分隔符差异恢复原文引用，保留修复回执；不能定位的模型改写仍拒绝。自动转录状态始终为待人工复核。

`label` 按完整文本预算组批、最多三批并发，不截断题面或解析。初判、全部正例与不确定项复判、分层 20% 负例复判以及分歧裁决均留存。裁判输入为公开文字与冻结转录、隔离的原始解析；不传排名或来源题号。原图在复核包中。裁判没有直接复查原始像素，转录遗漏可能影响银标，不能把结构校验称为人工认可。

`--limit` 用于小样本联调；再次执行同一阶段恢复未完成记录。`run --experiments E1,E2` 可选择实验。质量结果、计时与模型用量分开保存，任何通道失败均标记该组不完整。数据、配置或标签变化必须另建 `--dataset` 版本，运行标识与配置绑定，不覆盖旧结果。

100 题是有限候选域，BM25 可能返回不足 50 条，池外随机题也可能不足 10 道，均记录实际数量。缺少正例、前 20 存在未解决标签或配额不足时禁止冻结和正式评分。

分层联调可先运行 `pool --smoke`，六个格子各一条查询。缺少正例时运行 `repair`：先扩池到当前100题域的所有可检索候选并补标；只有全部判定完成且确定没有正例，才按固定种子替换整族两条查询，保持意图、模态和开发/测试归属。旧查询、标签与替换原因归档，之后重新 `prepare → pool → label`。存在未知标签时保留复核缺口，不借换题绕过争议。替换绝不读取质量分数，冻结后禁止执行。

当前交付版本从已有 `math_100_v1` 派生：

```powershell
backend/.venv/Scripts/python.exe -m evaluations.retrieval subset
```

`subset` 复用 100 道题、两个索引、20 条查询及其标签，新增 4 条纯文字同构查询，依据公开题面编排文本（由本次 AI 助手生成，未经人工审核）。筛选只读取正例可用性和标注完整性，不读取实验排名或指标。`subset_inputs.json` 与 `subset_selection.json` 记录来源哈希和题族重新划分。测试只有 6 个题族，并非独立未接触测试集，结果仅用于流程验证。`inherited_parent/` 成本单列此前 60 查询及替换工作，不能算作 24 查询的独占开销。

## 指标与 RAGAS

本地指标：Recall / Pooled Recall@5/10/20、nDCG@10、Precision@5/10、Hit@1/5/10、MRR@10、困难负例@10、Judged@20。相关阈值为等级 ≥2，nDCG 使用增益 $2^{grade}-1$。查询宏平均；按来源题族进行 2,000 次配对 bootstrap，给出 E3−E1、E3−E2、E4−E3 的差值和 95% 区间。开发集单列，主要比较只用测试集。

未标注不作为负例，影响前 20 的未知标签会阻止质量评分。返回不足 k 时主表 Precision 的分母仍是 k。RAGAS ID Precision 分母为实际返回数；空列表时 RAGAS 返回 NaN，报告为 null，本地约定零，两者明确区分。

```powershell
backend/.venv/Scripts/python.exe -m venv evaluations/retrieval/.venv-ragas
evaluations/retrieval/.venv-ragas/Scripts/python.exe -m pip install -r evaluations/retrieval/requirements-ragas.lock.txt
backend/.venv/Scripts/python.exe -m pytest evaluations/retrieval/tests -q
```

RAGAS 固定 0.4.3，单独进程调用 `IDBasedContextPrecision` 与 `IDBasedContextRecall`，不需要裁判模型或答案生成。未安装、进程失败或指标不一致均明确报告，不用本地结果冒充 RAGAS。

参考：[RAGAS ID Precision](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_precision/)、[ID Recall](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_recall/)。

## 产物与边界

- `common/`：数据导出、转录与查询、盲标、底层召回、冻结、指标、报告。
- `datasets/math_100_q24_v1/`：公开快照、查询、qrels、版本清单；`private/` 解析只用于标注，`assets/` 为图片快照，`work/` 为可恢复回执。
- `runs/<run_id>/`：四组逐查询结果、完整通道排名、一次预热后三轮轮换计时、RAGAS 输出。
- `reports/`：JSON、CSV、HTML；`review/`：含原图的标注依据和争议复核包。

计时使用相同预计算查询向量，只测 ES 召回、聚合和 RRF，不是在线端到端延迟。转录、编码、标签调用成本单列，缺失用量不估算成零，不虚构金额。ES 索引名带 `benchmark_retrieval_` 和内容配置哈希，不使用线上别名。旧 16 题试点及其 A/B/C/D 定义保留。

新增实验只需新增实验配置子目录；标签池必须重新扩充和冻结，再对所有组重跑。详细设计见 [DATASET_DESIGN.md](DATASET_DESIGN.md)。
