# 本地 Qwen 精排实测：100 题、24 查询

两套 Qwen 官方权重下载自 ModelScope，按官方清单校验了每个文件的大小和 SHA-256。模型放在仓库的 `data/cache/reranker_models/`，评测没有写业务索引、改生产配置或调用远程模型。

两组都处理冻结的 24 条查询。每条先保留 12 道文本向量候选，再按 IK BM25 名次去重补到 20；每个模型打分 480 个相同的题对。原 RRF 也在同一 20 道题上评分。文本版读取冻结题面和图示转录，图文版除此之外读取冻结原图。图文组共向模型传入 592 次图片（同一查询图会随不同题对重复输入）。按原任务意图区分找原题、找同结构和找同解法。

## 排序结果

测试集 12 条查询中，两条的文字模型前 5 有未标注题。下面仅比较所有方案均有标签的同一批 10 条，未把未知题当负例。Recall 以原标注池里的正例为分母。

| 方案 | Recall@5 | nDCG@5 | 含图查询 nDCG@5（6 条） |
|---|---:|---:|---:|
| 纯文本向量 | 75.00% | 0.7079 | 0.7244 |
| 同 20 道候选按原 IK RRF 排 | 60.00% | 0.5861 | 0.7104 |
| Qwen3-Reranker-0.6B | 80.00% | 0.7646 | 0.8398 |
| Qwen3-VL-Reranker-2B | 80.00% | 0.8190 | 0.9042 |

图文版单独和纯向量比较时，12 条测试查询的前 5 均有标签，nDCG@5 从 0.6860 提升至 0.7877；Recall@5 从 70.83% 到 75.00%。4 条同解法测试查询的 nDCG@5：纯向量 0.4761、文字精排 0.6743、图文精排 0.8058。原求和题族的 `math:1049` 之前在 RRF 排第 27、31 名，独立取候选后，文字版排第 2、1 名，图文版两条都排第 1。

样本少，查询和银标均来自已经用于诊断的旧数据。两个模型不只输入模态不同，参数规模也不同，不能把差值全归因于图片。也没有证据证明这两个模型优于旧的 DeepSeek 大模型精排，本轮没有调用后者。

## 本机耗时与显存

RTX 5070 Ti Laptop 12 GB，PyTorch 2.7.1+cu128，Transformers 4.57.6，BF16 + SDPA。逐个模型执行，每次查询对 20 道题打分。预热后计时，包含输入处理和 GPU 推理，不含初筛、模型加载、图片读盘或并发等待。显存为 PyTorch 峰值分配，含模型参数，不等于整个 GPU 的系统用量。

| 模型 | 单批题对 | P50 | P95 | 峰值已分配显存 |
|---|---:|---:|---:|---:|
| 文字 0.6B | 8 | 267 ms | 372 ms | 1.25 GiB |
| 图文 2B | 2 | 1.43 s | 1.98 s | 4.29 GiB |

建议先让 0.6B 做低延迟试用，图文 2B 优先验证图里有关键条件的题。下一轮用新题族验证，按查询和候选是否有关键图示分组，记录排序、漏召回、并发下 P95 和显存；本轮未改应用接线。

## 复现

```powershell
backend/.venv/Scripts/python.exe -B -m evaluations.retrieval.analyses.download_qwen
data/runtime/qwen_rerank_env/Scripts/python.exe -B -m evaluations.retrieval.analyses.qwen_replay text
data/runtime/qwen_rerank_env/Scripts/python.exe -B -m evaluations.retrieval.analyses.qwen_replay vl
data/runtime/qwen_rerank_env/Scripts/python.exe -B -m evaluations.retrieval.analyses.qwen_compare
```

评测虚拟环境复用本机已支持 CUDA 的 Torch，隔离安装了 `transformers==4.57.6` 和 `qwen-vl-utils==0.0.14`。运行文件不含凭据，模型推理只读取本地权重和冻结数据。

[对照 JSON](reports/math_100_q24_v1_qwen_comparison.json) · [文字逐查询](reports/math_100_q24_v1_qwen_text.json) · [图文逐查询](reports/math_100_q24_v1_qwen_vl.json)
