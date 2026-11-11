# Step 7 · 上传文件、Docker 分析与图表

## 使用

从项目根目录运行。当前机器已经安装上传页面依赖并构建了分析镜像。首次部署到其他机器时，需要先启动 Docker 并执行前两条准备命令。

```bash
.venv/bin/python -m pip install -r requirements.txt -r requirements-step7.txt
docker build -t rag-step7-sandbox:1 sandbox/step7
.venv/bin/python -m streamlit run app/step7_upload.py --server.address=127.0.0.1 --browser.gatherUsageStats=false
```

打开 [本地上传页](http://127.0.0.1:8501)，上传 CSV、XLSX 或 XLS，输入「有没有异常值并画图」，点击「开始分析」。也可询问「帮我统计一下」查看数据概况。多工作表文件需选择工作表。预览可调整表头起始行和表头行数，并显示解析后的列名。单文件上限为 10 MiB，单工作表最多 10,000 数据行和 50 列。[Streamlit 文件上传组件](https://docs.streamlit.io/develop/api-reference/widgets/st.file_uploader)

## 分析链路

LangGraph 将请求分为计划、沙箱分析、结果叙述三步。计划节点将自然语言匹配到异常值或统计任务。Docker 内的固定 pandas 工具负责读取、去除全空行、识别数值列、统计重复和无效数据。重复行保留在统计中。上传字节通过 stdin 进入本机容器，宿主接收的结果是经过 Pydantic 校验的 JSON。文字结论根据真实统计结果生成。文件和问题留在本机，无需模型 API。

每列至少有 4 个有效数值时，按 1.5 倍 IQR 标记疑似异常。箱线图使用全列统计值和实际观测到的须线，最多展示 6 个数值列，各列使用独立坐标轴。异常点保留数据行号和数值，供复核。

## 沙箱边界

运行前检查 Docker 连接为本机 Unix socket。容器只运行随镜像打包的固定分析代码。实际运行使用 `--network=none`、只读根文件系统、非 root 用户、移除全部 Linux capabilities、禁止提权、512 MiB 内存、1 CPU、64 进程、30 秒超时。容器无宿主目录挂载或 Docker socket，仅保留受限的临时内存目录。关闭容器日志驱动，上传不会作为日志写入 Docker。超时会尝试清理对应的容器。[Docker none 网络](https://docs.docker.com/engine/network/drivers/none/) · [Docker 容器参数](https://docs.docker.com/reference/cli/docker/container/run/)

构建镜像时只提交 `sandbox/step7/` 中的 worker、协议和依赖清单。安装依赖发生在构建期，运行期使用已构建的本地镜像。

## 验收

- 通过 Chrome 真实上传 CSV、XLSX 和 XLS 三种合成文件并提交「有没有异常值并画图」。三种格式均返回文字解读和 Plotly 图，标记 **收入列第 6 条数据的 110**，判定区间为 **[8, 14]**。
- 通过 Docker inspect 和容器内探针确认网络断开、宿主测试文件及 Docker socket 不可见、镜像目录不可写。
- 实际 Docker 边界测试覆盖零 IQR、小样本、全空行、统计模式、损坏 Excel 和超出行数限制。两套项目环境均通过 **40 项回归测试**。

浏览器截图、合成样例、分析结果和 Docker 隔离记录已归档到 `evaluation/results/step7-1/`。

## 文件筛选与排名

产品页面的「文件分析」支持按数值比较、类别集合、含端点的数值范围或字面文本筛选。多个条件同时成立才会保留该行。先按条件筛选，再计算或排名。

- 按省份等类别做 TopN 时，先对每组计算指定指标，再对组排序并取前 N。整体汇总仍基于筛选后的全部数据。
- 按原表明细做 TopN 时，直接对筛选后的明细排序。并列时，分组排名按分组键顺序，明细排名按原表数据行号。缺少有效排序指标的记录或分组不会参加排名。
- 单次 TopN 上限 100。图表沿用对应结果表的条数和顺序，并带有筛选口径与指标单位。分组较多时，页面标明展示条数和完整结果条数。
- 结果标明工作表、筛选条件、参与行数和排名条数。条件无匹配、排序依据不明确、请求超过上限或已识别的条件未出现在计划中时，页面给出相应提示。
- 对字段和范围可唯一确定的单年排名，系统可直接构造本地计划。其他任务可由模型依据问题、工作表名、列名和数值列索引生成计划。原始表格留在本机容器中。

重建后从项目根目录运行专项评测。附加 `--workbook` 会在无网络容器中读取原表并和独立 pandas 计算核对，结果存入 `evaluation/results/file-selection-*/results.json`。评测只写统计结果，题集不含原表数据。

```bash
docker compose build sandbox-image
.venv/bin/python -m evaluation.run_file_selection
```

### 专项评测

公开复现入口提供 15 道合成用例，覆盖单条件和多条件筛选、无匹配、非法计划、分组和明细 Top N、超过 25 条的图表和指定年份的覆盖分析。测试用独立 pandas 计算核对参与行数、聚合值、排名顺序和图表条数。执行记录写入本地 `evaluation/results/file-selection-*/results.json`。
