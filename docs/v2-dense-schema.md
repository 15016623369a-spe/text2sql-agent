# V2 BGE-M3 + Chroma 向量召回

## 接入方式

V2 的默认路径仍为 BM25 Top-4、表名与字典值匹配和外键桥接。加上 `--dense-schema` 后，本地 BGE-M3 为每张表和每个字段生成向量。索引使用 SQLite 表列名和 BIRD 的字段说明、官方值编码。数据库行、参考 SQL 和答案均不入向量库。模型权重固定到 BAAI 官方提交，校验下载文件 SHA256 后转换为 safetensors，检索时只加载本地文件。[BGE-M3 官方模型](https://huggingface.co/BAAI/bge-m3/tree/5617a9f61b028005a4858fdac845db406aefb181)

向量和表列元数据保存在本地 Chroma。分别检索表文档与列文档，按每张表的最小余弦距离去重，取向量 Top-4。最多追加两张基线尚未覆盖的表，然后沿已有外键图补桥接表。保留基线的已选表。Chroma 集合名由模型版本、索引版本与文档内容计算，内容变化时生成新集合，索引未写完时可安全重建。[Chroma 距离配置](https://docs.trychroma.com/docs/collections/configure)

## 离线召回对照

检索规则在 50 道开发题上接通后冻结。在原 200 道留出题和另 250 道未用于规则调试的题上，使用同一 SQLite 快照、题目和官方证据比较。指标以参考 SQL 的表列读集计分。两个集合均来自现有的 11 个数据库。默认 BM25 初筛的 Recall@4 保持一致，下述结果为候选扩展及外键桥接后的最终覆盖率。

- **原留出 200 题**。最终表召回 **98.09% → 99.36%**，最终列召回 **98.27% → 99.42%**，漏表题 **8 → 3**。例如第 121 题由向量召回补回了 `users`，第 241 题补回 `results` 后沿外键找回 `drivers`。结构 DDL 字节 **592,510 → 630,201（+6.36%）**。
- **另一组 250 题**。最终表召回 **98.95% → 99.48%**，最终列召回 **99.04% → 99.49%**，漏表题 **6 → 3**。结构 DDL 字节 **760,400 → 797,562（+4.89%）**。
- **相近提示体积对照**。原留出集只把 BM25 的 K 调到 5，仍有 **6 道**漏表，DDL 为 **625,928** 字节。Top-4 加向量分支只漏 **3 道**，DDL 多约 **0.68%**。另一组 250 题上，BM25 Top-5 漏 **4 道**，向量方案漏 **3 道**且 DDL 少约 **0.46%**。
- 单独看向量 Top-4，200 题和 250 题的表召回分别为 **84.50%**、**84.67%**。本轮的收益来自向量与原检索路径互补。

## 同一 50 道开发题的执行对照

两组均开启自动补注，用 `deepseek-flash`、温度 0、6 个并发，执行相同的只读校验。逐题核对，向量分支在 18 题改变了提示，其余 32 题的提示完全相同。EX 通过 [BIRD 官方函数](https://github.com/bird-bench/mini_dev/blob/1c49a375e76c471e218564da3f4617513d33e371/evaluation/evaluation_ex.py) 复核。

- **默认对照**为 **26/50（52%）**，输入 **50,454** token，总计 **56,832** token。
- **向量分支**为 **27/50（54%）**，输入 **52,638** token（**+4.33%**），总计 **59,543** token（**+4.77%**）。
- 第 187 题由错变对，其余题的 EX 相同。50 题的差异为净增一题，整体执行增益仍待验证。基线第 414 题首次连接失败，原提示补测后纳入汇总。补测校验脚本一度误判 JSON 容器类型差异并丢弃结果，修正后重新补测。原始失败日志一并归档。P95 从 **2.90 秒**到 **3.88 秒**，向量组与离线召回验证并行，这个观测值包含本机资源竞争。

## 使用和复现

当前机器已把模型和 11 个数据库的 Chroma 索引放在 `data/bird/.retrieval/`，并在项目 `.venv` 安装可选依赖。新机器先按项目原有方式准备 BIRD 数据，再运行：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-dense.txt
.venv/bin/python -m evaluation.prepare_dense
.venv/bin/python -m unittest discover -s tests -v

.venv/bin/python -m evaluation.run_schema_recall --split eval --top-k 4 5 6 --dense-schema
.venv/bin/python -m evaluation.run_v2 --auto-column-notes --workers 6
.venv/bin/python -m evaluation.run_v2 --auto-column-notes --workers 6 --dense-schema
```

首次模型下载约 **2.27 GB**。准备完成后，离线召回可断网运行。可用 `--dense-root` 指定其他模型和索引目录。基线和向量环境的 **25 项测试均通过**。实验记录在 `evaluation/results/v2-dense-1/`。
