# V3 最小执行反馈与恢复

本文保留第一版恢复循环的实测。当前 V3 的独立 dry run、Pydantic 和新基线对照见 [Step 5 试跑纠错](v3-step5-dry-run.md)。

V3 沿用当前 V2 的检索、可选补注和首轮提示，用 LangGraph 编排 SQL 生成、只读执行和重试决策。它使用原有的只读 SQLite 防护与每次查询 5 秒期限。

- **SQL 修复。** 语法错误、列名歧义，或者报错列已在提示中时，携带失败 SQL 和 SQLite 错误，按同一 schema 重新生成。
- **补检索。** 缺表或缺列时，在当前数据库的表列元数据中定位。可定位到提示以外的候选表时，补入最多两张候选表及其声明外键路径，再用新 schema 重生成。缺表名仅在唯一高相似匹配时作近似定位。
- **结束。** SQL 执行成功直接提交结果，空结果也算执行成功。违规 SQL、超时、请求失败或不可恢复的错误直接结束。修复和补检索共用最多三次重试，同一个 SQL 和同一错误再次出现会提前结束。

每次尝试都记录提示、SQL、错误类别、实际动作、追加表、耗时和 token。参考 SQL 和执行结果行留在本地，只有可恢复的执行错误和失败 SQL 会回到模型提示。成功执行但语义错误的结果依旧由离线 EX 计分。

## 50 道开发题的 V2 对照

固定当前检索、自动补注、`deepseek-flash`、温度 0、6 个并发、同一题集和超时。逐题核验 V2 与 V3 首轮提示完全相同。两组均用 [BIRD 官方 EX 评分函数](https://github.com/bird-bench/mini_dev/blob/1c49a375e76c471e218564da3f4617513d33e371/evaluation/evaluation_ex.py) 重新计算。

- **V2** 为 **27/50（54%）**，输入 **50,454** token，总计 **57,133** token。一次执行错误为第 156 题的语法错误。
- **V3** 首轮与最终均为 **27/50（54%）**，输入 **50,454** token，总计 **56,975** token。一次执行错误为第 491 题超时，按规则退出。本轮没有发生重试，也未观察到恢复带来的整体 EX 增益。

两组独立调用中，有 1 道由错变对、1 道由对变错。这两题均未触发重试。

## 定向验收

自动化测试通过受控的模型输出，真实执行 SQLite，覆盖语法修复、缺列补表并沿外键桥接、共享预算耗尽、重复错误提前退出，以及违规语句和超时的终止分支。回归测试共 **22/22** 通过。

真实模型验收固定注入首轮错误 SQL，随后交给 `deepseek-flash` 恢复。合成用例的语法修复和缺列补检索均在一次重试后得到正确结果。缺列用例追加了 `mapping` 和 `regions` 两张表。

额外重放 V2 第 156 题的真实语法错误。一次重试后 SQL 可以执行，但模型仍使用 `YYYY-MM` 比较存为 `YYYYMM` 的月份，EX 仍为 0。这条定向重放单独记录。

## 复现

```bash
conda activate chat-data-agent
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m evaluation.run_v2 --auto-column-notes --workers 6
python -m evaluation.run_v3 --auto-column-notes --workers 6 --max-recoveries 3
```

V3 的 `--max-recoveries` 取值 0–3，默认 3。两版均默认使用 50 道开发题，支持 `--split eval`。本次对照、逐题日志和定向验收记录保存在 `evaluation/results/v3-minimal-dev-1/`。LangGraph 条件边参考 [官方 Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)。
