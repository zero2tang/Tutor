# Tutor 理科分词词典

索引侧使用 `ik_max_word`，查询侧使用 `ik_smart`。`tutor_math.dic` 和
`tutor_physics.dic` 是 UTF-8 文本，每行一个术语。文件分别维护，IK 在节点内合并加载。
仅把教材和题库中适合作为完整搜索词的短语加入词典。同义词和公式等价关系另外处理。

词条来自 Tutor 数学知识点、力学课程概念和筛选后的标准术语。公开术语参考：

- 國家教育研究院 [2015] [數學名詞－兩岸中小學教科書學術名詞](https://data.gov.tw/dataset/15444)
- 國家教育研究院 [2015] [物理學名詞－兩岸中小學教科書學術名詞](https://data.gov.tw/en/datasets/15318)

上述公开数据依[政府資料開放授權條款第 1 版](https://data.gov.tw/license)释出，可依条款利用。
词典采用与 Tutor 教材一致的简体名称。

从仓库根目录执行。本机示例容器为 `elasticsearch-9.3.2`，请先暂停 Tutor 的索引写入：

```powershell
.\scripts\maintenance\install_es_ik.ps1 -Container elasticsearch-9.3.2
# 等待 ES 恢复后：先查看计划，再复制并切换别名。
.\backend\.venv\Scripts\python.exe scripts/maintenance/migrate_es_ik.py
.\backend\.venv\Scripts\python.exe scripts/maintenance/migrate_es_ik.py --apply
```

安装脚本会重启所选容器，插件和词典保存在容器内；重建容器时须重新安装。
迁移脚本使用 Tutor 的 `ES_URL`。它保留原索引，复制已有文档和向量，校验数量后一次切换在用别名。
首次部署没有旧索引，安装 IK 后直接构建课程投影。以后若修改索引分词用的词典，
已有索引需重建以应用新词。用于对比实验的 CJK 基线仍可单独使用。
