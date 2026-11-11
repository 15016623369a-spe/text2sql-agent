# Step 5 · V3 试跑纠错循环

## 生成、试跑与执行

V3 用 LangGraph 将模型生成、dry run 和正式执行设为独立节点。Pydantic 严格验证模型给出的单字段 `{"sql": "..."}`，并约束试跑结果、SQLite 错误和重试反馈。空白 SQL、错误类型和额外键在进入数据库前即被拒绝。[Pydantic 严格模式](https://pydantic.dev/docs/validation/latest/concepts/strict_mode/)

dry run 在只读连接内用 `EXPLAIN` 编译 SQL。它检查语法、表列引用和只读权限，无需解析执行计划文本。由于某些命令可以在编译时跳过授权回调，试跑还要求 SQLite 明确识别到查询。正式执行继续使用独立的只读连接和 5 秒期限。[SQLite EXPLAIN](https://www.sqlite.org/lang_explain.html)

- 语法错误和当前 schema 内的引用错误进入 SQL 修复。命中提示外的表列时，先补检索并沿外键补齐路径，再生成。
- 失败 SQL、错误阶段、类别、错误详情和重试轮次作为结构化反馈交给模型。参考 SQL 和结果行仅参与本地评分。
- 修复和补检索共享 LangGraph 状态中的 `retry_count`，最多重试 3 次。第三次重试仍经过试跑和执行。重复 SQL 加相同错误、超时、违规或达到预算时退出。
- 试跑通过后才正式执行。合法的空结果也可以通过执行，语义正确性由 BIRD EX 另外检查。

## 50 题同条件实测

冻结相同开发题、模型 `deepseek-flash`、温度 0、6 并发、BM25 + BGE-M3/Chroma、自动补注和只读查询限制。两组首轮提示逐题完全相同。先试跑 10 题，消耗 **12,867 输入 token**，再运行完整 50 题。正式 EX 用 [BIRD 官方评分函数](https://github.com/bird-bench/mini_dev/blob/1c49a375e76c471e218564da3f4617513d33e371/evaluation/evaluation_ex.py) 复核。

- **V2** 为 **26/50（52%）**，调用 **50 次**。输入 **52,638**、输出 **6,955**，合计 **59,593 token**。P95 **5.52 秒**。
- **V3** 首轮与最终均为 **26/50（52%）**，调用 **51 次**，平均 **0.02 次重试/题**。输入 **53,268（+1.20%）**、输出 **7,153**，合计 **60,421（+1.39%）**。P95 **4.02 秒**。P95 是两次独立模型调用的观察值，本次只读 dry run 节点本身的 P95 为 **1.83 毫秒**。
- V3 首轮有两题无法成功执行。第 156 题在试跑发现语法错误，重试消耗 **630 输入 + 95 输出 token**，之后能执行，但仍用 `YYYY-MM` 比较存为 `YYYYMM` 的月份，EX 为 0。第 491 题试跑通过，正式执行超时并终止。本轮恢复使一条 SQL 恢复到可执行，首轮失败到最终 EX 的恢复为 **0/2**。
- 按 2026-11-10 北京时间高峰价及返回的缓存命中、未命中和输出 token 估算，V2 **¥0.07738**、V3 **¥0.07947**，平均每题约 **¥0.00155 → ¥0.00159**。仅计入正式对照的请求。[DeepSeek 定价](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)

## 演示与回归

- 合成演示固定注入 `SELECT FROM notes`。试跑拒绝后，真实模型一次修复为合法查询，执行返回预期的 `first`。
- 用 V2 的失败 SQL 做定向重放。第 **146** 题首轮引用了不存在的 `p.CreationDate`，试跑捕获后，一次真实模型重试使用了 `postLinks.CreationDate`，最终结果与参考答案一致。第 **156** 题修复语法后仍存在月份编码问题。定向结果单列，正式 50 题得分按自然生成的候选计算。
- 原 conda 环境和项目 `.venv` 都跑过 **29 项测试**。覆盖了试跑失败时跳过执行、空白和错误 JSON 拦截、第三次重试可执行、缺列补检索、重复提前停止、超时和写语句拦截。

项目根目录运行：

```bash
.venv/bin/python -m evaluation.demo_v3
.venv/bin/python -m evaluation.demo_v3 --live
.venv/bin/python -m evaluation.run_v2 --dense-schema --auto-column-notes --workers 6
.venv/bin/python -m evaluation.run_v3 --dense-schema --auto-column-notes --workers 6 --max-recoveries 3
.venv/bin/python -m unittest discover -s tests -v
```

逐题轨迹、正式对照、估算费率和演示记录在 `evaluation/results/v3-step5-1/`。
