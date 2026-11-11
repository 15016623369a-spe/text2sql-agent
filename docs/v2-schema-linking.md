# V2 Schema Linking

在项目根目录激活 `chat-data-agent` 环境后运行：

```bash
python -m unittest discover -s tests
python -m evaluation.run_v2
```

默认使用 BM25 选 4 张候选表。索引包含表名、列名和 BIRD 字段说明。检索使用题目及官方 `evidence`，补充命中表名或小型字典值的候选表，再沿已声明的外键最短路径补齐中间表。送给模型的是入选表的完整 DDL，并保留 V1 的表顺序。没有词项命中时使用完整 schema。新旧检索的留出集复测和补注修复见 [检索修复报告](v2-schema-recall-fix.md)。下方的 EX 和 token 数字来自修复前的 V2。

参考 SQL 只用于本地评测。SQLite 在准备参考 SQL 时记录读取的表和列。Recall@4 按所有题的参考对象合并统计，同一题内去重。外键诊断记录参考 SQL 读取了已声明外键的两端、但其中一端的列被筛除的情形。使用不同但等价的 SQL 路径仍可能通过 EX。

## 2026-11-10 开发集对照

V1 和 V2 使用同一批 50 题、同一数据版本、`deepseek-flash`、温度 0、同一提示模板、同一证据与 5 秒 SQLite 查询期限。正式评测清单保留到配置冻结。用固定版本的官方 `calculate_ex` 对两份逐题结果复核。[BIRD 官方 EX 评分函数](https://github.com/bird-bench/mini_dev/blob/1c49a375e76c471e218564da3f4617513d33e371/evaluation/evaluation_ex.py)

- **执行准确率**从 V1 的 21/50（42%）变为 V2 的 19/50（38%）。3 题由错转对，5 题由对转错。V2 有 28 题结果不一致、2 题引用不存在的列、1 题超时。
- **输入 token** 从 51,236 降至 42,175，减少 **17.7%**。平均每题提示中的表从 6.86 张降至 4.14 张。P95 从 3.18 秒变为 3.07 秒。
- **表 Recall@4** 为 **87.3%**，补齐外键路径后为 **92.4%**。**列 Recall@4** 为 **90.1%**，补齐后为 **94.0%**。补表找回了 5 道题中的 6 次参考表命中，最终仍有 7 道题缺参考表，其中 6 道缺少至少一组参考 SQL 所读的已声明外键列。
- 31 题的提示输入因筛表发生变化，这部分从 16/31 变为 15/31。另有 19 题的模型输入逐字相同，仍出现 3 题得分变化，从 5/19 变为 4/19。这次整体 4 个百分点的差值同时包含输入变化和重复调用的波动。

## 逐题复盘

- **ID 246，Formula 1**。初筛保留了 `results`，漏掉了 `drivers` 和 `races`。外键路径补齐两张表后，生成 SQL 通过了 EX。
- **ID 215，Financial**。初筛包含 `account`，但两两连通所需的路径没有纳入 `district`。参考 SQL 用 `account.district_id` 连接 `district.district_id`。筛表后的 SQL 未通过 EX。
- **ID 187，European Football**。候选表缺 `Country`，生成 SQL 从只含国家 ID 和计数的 CTE 读取 `name`，报 `no such column: c.name`。
- **ID 146，Codebase Community**。参考表和列都已覆盖，模型仍把日期字段写成 `p.CreationDate`，引用了不存在的列。这是生成阶段的错误。
- **ID 107，Card Games**。参考 SQL 使用了未召回的 `rulings`，而生成 SQL 仍得到相同的结果。该题说明参考表覆盖率和执行准确率衡量的是不同问题。

V2 运行记录保存在 `evaluation/results/v2-20261109T160841738680Z/`。逐题日志中的 `schema_linking` 包含初筛表、补齐的中间表、最终表及参考表列的缺失项。

可选的 BGE-M3 + Chroma 分支、与默认方案的离线召回对照及实际 token 记录见 [V2 向量召回](v2-dense-schema.md)。
