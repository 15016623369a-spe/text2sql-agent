# V1 完整 schema 基线

在项目根目录激活 `chat-data-agent` 环境并配置项目 `.env` 后运行：

```bash
python -m unittest discover -s tests
python -m evaluation.run_v1 --limit 10
python -m evaluation.run_v1
```

运行器只使用 `evaluation/splits/dev_ids.json`。每次运行写入一个独立的 `evaluation/results/v1-*/` 目录，也可用 `--output-dir` 指定新目录。目录中的 `run.json` 记录题目和配置，`questions.jsonl` 记录模型输入、生成 SQL、执行结果、耗时和 token，`summary.json` 汇总指标。

基线对每题提供当前库的全部表和视图 DDL、题目及 BIRD 官方 `evidence`。模型只调用一次，要求返回带 `sql` 字段的 JSON。生成 SQL 经过 SQLite 只读连接、授权回调、单语句检查和 5 秒查询期限。参考 SQL 在本地执行，只进入评分。EX 按官方函数比较完整结果行的集合。延迟覆盖 schema 读取、模型调用和生成 SQL 的执行，参考答案评分在该计时之外。

## 2026-11-09 开发集实测

- 使用 `deepseek-flash`，温度为 0，关闭思考模式。数据版本见 [BIRD 数据来源](../data/bird/SOURCES.md)。
- 10 题试跑 EX 为 3/10，P95 为 3.79 秒。完整开发集另外运行 50 题，EX 为 **21/50（42%）**，P95 为 **3.18 秒**。两次结果分别保存。
- 50 题中有 21 题结果一致、28 题结果不一致、1 题 SQL 执行超时。题目覆盖 11 个库，其中 47 道 challenging、3 道 moderate。
- 50 题合计 58,000 token，其中缓存命中输入 32,000、未命中输入 19,236、输出 6,764。按当时空闲时段的公开费率估算，合计 **¥0.046932**，平均每题 **¥0.000939**。[DeepSeek 费率](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)
- 用固定版本的官方 `calculate_ex` 复核两次运行的逐题结果，分别得到 3/10 和 21/50。[BIRD EX 评分函数](https://github.com/bird-bench/mini_dev/blob/1c49a375e76c471e218564da3f4617513d33e371/evaluation/evaluation_ex.py)

试跑目录为 `evaluation/results/v1-20261109T151618465357Z/`，完整开发集目录为 `evaluation/results/v1-20261109T151709285868Z/`。正式评测题清单留到配置冻结后使用。

## 失败案例

- **ID 9，california_schools**。查询按原始数字字符串匹配 `CDSCode`，返回 0 行。库里三个目标代码是带前导零的文本，参考查询转成整数后匹配并返回 1 行。字段已经在输入里，缺的是值的存储规则。
- **ID 15，california_schools**。模型把州级特许学校类别映射到 `FundingType`，返回 0 行。只读查询核实该类别存于 `DOCType`，对应的 `FundingType` 为直接拨款或空值。字段描述或类别值检索可以为这个映射提供依据。
- **ID 67，card_games**。模型对一张牌不同印次的裁定记录直接计数，返回 46。参考 SQL 按裁定文本去重，结果是 2。涉及的表和连接键都已给出，问题出在统计粒度。
- **ID 491，toxicology**。生成 SQL 在每个分子上执行多个相关子查询，还在窗口排序中重复计算连接聚合，触发 5 秒截止。参考 SQL 先聚合碳键再参与排序。该题为执行反馈阶段保留了一个明确的超时案例。
