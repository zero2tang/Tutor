# E3：混合 RAG

启用 `keyword` 与 `text_vector`，各取前 50 道题，再做等权 RRF：$s(d)=\sum_c 1/(60+r_c(d))$。分块先聚合到题目，每通道每题最多一票。不调用图文编码器、重排或图谱。

共享运行器：`python -m evaluations.retrieval run --experiments E3`。原始通道分数与完整排名写入每条结果，便于分析增益来源。
