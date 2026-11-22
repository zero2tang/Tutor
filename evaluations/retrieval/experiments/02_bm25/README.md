# E2：单关键词倒排索引

仅启用 `keyword`，使用独立 benchmark ES 文本索引及 CJK 分词器，按 BM25 分数排序。在线查询不调用任何向量编码器；共享索引准备阶段生成其他实验所需的向量，不计入 E2 查询成本。

共享运行器：`python -m evaluations.retrieval run --experiments E2`。上限 50 道候选；没有关键词命中可以返回不足 50 道，不从其他通道补齐。
