# 持续集成与模型夜间回归

## 提交检查

`Application CI` 在提交和拉取请求上运行五个任务。宿主单测运行完整 `unittest`，文件解析与公式核对测试另在实际 Docker worker 中运行。30 个合成会话通过产品 API 回放录制的真实模型响应，覆盖 74 轮，检查返回类型、关键槽位、结果及相关 SQL。请求按哈希匹配，录制内容不包含密钥或真实用户数据。Chromium 冒烟验证刷新恢复、澄清、审批、CSV 上传与图表。PostgreSQL 任务启动十表示例库，验证 API 查询结果、选表边界、CSV、追踪和写入拒绝。提交检查无需模型密钥。

全部五项通过后，汇总检查 `Week 1 required checks` 才会通过。启用默认分支保护时可将该检查设为合并条件。

使用以下命令在本机复现。Chromium 也可通过 `CHROMIUM_EXECUTABLE` 指向已安装的浏览器。

```bash
.venv/bin/python -m pip install -r requirements.txt -r requirements-step7.txt -r requirements-step8.txt playwright==1.65.0
.venv/bin/python -m playwright install chromium
.venv/bin/python -m unittest discover -s tests -v
docker build -t rag-step7-sandbox:1 sandbox/step7
docker run --rm -i --network none --entrypoint python rag-step7-sandbox:1 - < tests/test_file_tabular.py
docker run --rm -i --network none --entrypoint python rag-step7-sandbox:1 - < tests/test_file_formula_worker.py
RAG_TEST_PYTHON=.venv/bin/python bash .github/scripts/run_checks.sh followup
RAG_TEST_PYTHON=.venv/bin/python bash .github/scripts/run_checks.sh browser
```

## 夜间真模型

`Nightly live model regression` 在默认分支每天北京时间 02:17 计划运行，也支持在 Actions 手动触发。仓库需要一个 `LLM_API_KEY` Actions secret。夜间跑产品 SQL 路径的 50 道 BIRD 开发题，并用真实模型重跑 30 个合成会话。确定性澄清和筛选照常由本地规则处理。

BIRD 数据在运行时从固定官方快照下载并校验 SHA256。夜间顺序跑三遍同一组 50 题，每遍使用 3 个 worker。每批保留三个正确题数和三个 P95，验收取各自的中位数。题目变化按三次中至少两次答对的名单计算。对照冻结的产品开发集基准，并从最近五次成功运行中选取首份可比报告。

- 正确题数中位数相对任一基准下降 **5 题及以上**，判为回归。
- BIRD 单题 P95 的三次中位数超过任一可比基准的 `max(基准 P95 × 1.25, 基准 P95 + 0.5 秒)`，判为回归。该指标从产品查询核心计时，主机与模型服务状态会影响它。
- 合成对话必须 **30/30**，按服务端追踪计算的轮次 P95 必须 **≤ 6 秒**。缺失、重复、配置不一致或含基础设施失败的 BIRD 运行会使本次检查失败。

冻结基准来自三轮隔离运行，分别答对 27、26、27 题，P95 中位数为 9.00 秒。冻结基准的相对 P95 门槛为 11.25 秒。缺失、损坏或不兼容的冻结文件会触发失败。GitHub artifact 包含脱敏配置、三轮逐题状态和合成对话结果。参见 [产品开发集基准](../evaluation/baselines/product_dev.json)、[项目数据快照](../data/bird/SOURCES.md) 和 [BIRD 官方数据入口](https://bird-bench.github.io/)。

本机可使用已有 `.env` 和已校验的 BIRD 数据运行同一组评测。

```bash
nightly_dir=$(mktemp -d)
for repetition in 1 2 3; do
  .venv/bin/python -m evaluation.run_product --split dev --workers 3 \
    --output-dir "$nightly_dir/bird-$repetition" || exit 1
done
LLM_MODEL=deepseek-v4-pro LLM_TEMPERATURE=0 RAG_TEST_PYTHON=.venv/bin/python \
  bash .github/scripts/run_checks.sh nightly --output "$nightly_dir/artifacts/conversations.json"
.venv/bin/python .github/scripts/nightly_report.py \
  --bird-dir "$nightly_dir/bird-1" --bird-dir "$nightly_dir/bird-2" --bird-dir "$nightly_dir/bird-3" \
  --baseline evaluation/baselines/product_dev.json --output-dir "$nightly_dir/artifacts"
```

更新基准时，将同三个 `--bird-dir` 交给 `nightly_report.py --write-baseline evaluation/baselines/product_dev.json` 可更新冻结基准。

## 产品路径 BIRD 评测

`evaluation.run_product` 直接调用 `/api/v1/query` 使用的 `query_database`，包括 BM25、自动字段补注、SQL 检查与有界恢复。记录同一次 SQL 执行的完整类型化结果，以集合相等计算 EX。评测使用题库提供的 evidence。产品只返回了部分结果时，该题计入 EX 分母，状态记为 `result_limit`。未回答也计入 EX 分母。评测基础设施或参考 SQL 失败会使整次结果标记为无效。

默认使用 DeepSeek V4 Pro、温度 0、模型请求超时 30 秒、SQLite 单次执行超时 5 秒。`--dense-schema` 可显式开启已准备的向量召回。每次运行写入 `run.json`、`questions.jsonl` 和 `summary.json`，记录数据与划分哈希、提示词和源码指纹、执行与回答覆盖率、错误率、P50/P95、token 及按库和难度的分层指标。逐题 SQL 日志留在本机。`python -m evaluation.product_diagnostics <运行目录>` 为开发集生成错误排查线索。

### 重复评测

旧版对应引入这三项改动前的产品 SQL 路径。评测固定为 50 道开发题与 200 道留出题。开发集每轮用 3 个 worker，留出集每轮用 5 个 worker。留出集复用上一轮同一对冻结源码的有效记录，并各补跑两轮。区间是同一批题在重复调用中的观测范围。

- 开发集旧版答对 23、22、20 题（每轮 50 题），EX 中位数 44%，范围 40%–46%。当前版答对 27、26、27 题（每轮 50 题），EX 中位数 54%，范围 52%–54%。
- 留出集旧版答对 68、60、65 题（每轮 200 题），EX 中位数 32.5%，范围 30%–34%。当前版答对 79、79、77 题（每轮 200 题），EX 中位数 39.5%，范围 38.5%–39.5%。
- 产品查询核心 P95 取三轮中位数，开发集旧版 6.59 秒、当前版 9.00 秒，留出集旧版 4.50 秒、当前版 5.08 秒。当前版开发集 150 次请求中有 70 次结果不符。留出集 600 次请求中有 343 次结果不符、11 次重复 SQL 错误、7 次超时和 4 次模型输出错误。这里的次数包含同一题的重复运行。合成对话的 74 轮另行计时，轮次 P95 为 1.30 秒。
- 首批并行尝试触发模型限流，无效批次与其中两批有效结果均单独留档。发布数字使用隔离运行。完整的逐题、用量和耗时记录保存在本机 `evaluation/results/product-stability/`。

### 消融与语义检查

`--profile full` 启用全部功能。 `baseline` 关闭三项功能。 `no-value-hints`、`no-prompt`、`no-semantics` 分别只关闭值编码线索、产品 SQL 提示和语义检查。使用固定 50 题与相同模型参数，每组重复三次。开发集各配置完成后可复现汇总。

```bash
dev_dir=$(mktemp -d)
for repetition in 1 2 3; do
  for profile in full baseline no-value-hints no-prompt no-semantics; do
    .venv/bin/python -m evaluation.run_product --split dev --workers 3 --profile "$profile" \
      --output-dir "$dev_dir/$profile-$repetition" || exit 1
  done
done
.venv/bin/python -m evaluation.compare_product_profiles "$dev_dir"/* --output "$dev_dir/comparison.json"
```

复测留出集时，用 `--split eval --workers 5`，每轮只跑 `baseline` 和 `full`，完成三轮后用同一汇总命令比较。

比较完整配置与单项关闭配置的 EX 中位数，并查看每次逐题转对、转错和 token 变化。它估计的是该功能在其他功能开启时的贡献。两组的模型响应独立生成，三个差值不可直接相加。

- 关闭值编码线索后答对 26、27、26 题（每轮 50 题），EX 中位数 52%，范围 52%–54%。完整配置减去该组的 EX 中位数为 +2 pp，三次配对差值范围为 -2 至 +2 pp。总 token 中位数为 71,666。
- 关闭产品 SQL 提示后答对 25、23、25 题（每轮 50 题），EX 中位数 50%，范围 46%–50%。完整配置减去该组的 EX 中位数为 +4 pp，三次配对差值范围为 +4 至 +6 pp。总 token 中位数为 62,032。
- 关闭语义检查后答对 25、25、26 题（每轮 50 题），EX 中位数 50%，范围 50%–52%。独立生成 SQL 的两组中位数相差 +4 pp，总 token 中位数为 75,321。语义检查的贡献以同 SQL 回放为准。
- 开发集每轮总 token 中位数为旧版 60,418、完整配置 73,709。

语义检查另用同一 SQL 做配对验证。三轮共重放 149 条已完成的候选 SQL，规则拦下 0 个错答和 0 个正确答案。各轮正确题数为 25→25、25→25、26→26。回放覆盖已记录的最终候选。错答转为暂缓回答在 BIRD EX 中仍计零分，分开记录拦截与准确率。

```bash
.venv/bin/python -m evaluation.replay_product_semantics "$dev_dir/no-semantics-1" \
  --output "$dev_dir/semantics-shadow.json"
.venv/bin/python -m evaluation.run_semantic_cases --output "$dev_dir/semantics-targeted.json"
```

14 道定向题固定问题和模型 SQL，分别走有、无语义检查的产品路径并实际执行 SQLite。规则拦下 5 个错误答案，覆盖漏分组、计数返回空集、NULL 或负数。它也误拦了 1 个正确的单地区汇总，漏过 1 个形式合法但数值错误的计数。关闭时返回 8 个正确答案和 6 个错误答案，开启时返回 7 个正确答案和 1 个错误答案，另外 6 题暂缓回答。定向题用于回归拦截与误拦，BIRD EX 单独计算。
