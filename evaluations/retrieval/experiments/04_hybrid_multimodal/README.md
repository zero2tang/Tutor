# E4：混合 RAG＋图文融合向量

在 E3 上添加 `multimodal` 通道：`qwen3-vl-embedding` / 1024 维，真实题图和同一冻结文本共同编码到独立空间。全部候选题均被该模型编码；纯文字查询也走图文编码器。各通道前 50、等权 RRF、常数 60。

共享运行器：`python -m evaluations.retrieval run --experiments E4`。额外的编码器与候选预算也是实验变量，所以 E4−E3 不能解释为纯像素增益；不预设其一定胜出。
