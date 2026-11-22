# E1：单语义 embedding

仅启用 `text_vector`，用 `text-embedding-v4` / 1024 维检索独立文本空间。输入是公共题面和冻结图示转录，排序保持向量相似度顺序。不得调用 BM25、图文编码器、重排、图谱或题号直达。

共享运行器：`python -m evaluations.retrieval run --experiments E1`。每通道 50 道候选，按题目去重；配置见 `config.json`。
