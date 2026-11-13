# 见数系统架构

应用提供两个数据入口。数据库问数先选取相关表列，再生成、验证和执行 SQLite 或 PostgreSQL 查询。文件分析先确认工作表与表头，再执行受约束的表格操作。两条路径都将生成计划与实际执行分开，结果包含可核对的查询或计算口径。

## 服务和数据边界

| 组件 | 入口或存储 | 职责 |
| --- | --- | --- |
| Streamlit | `app/product_ui.py` | 对话、数据源选择、文件预览、表格和图表、审批操作 |
| FastAPI | `app/product_api.py` | 请求验证、工作流流式返回、入口身份和并发限制 |
| LangGraph | `app/conversation.py`、`app/recovery_graph.py` | 对话澄清与审批中断、SQL 试跑和恢复分支 |
| SQLite | 演示订单、只读数据库、Checkpointer、追踪 | 分别保存示例数据、会话和运行元数据 |
| PostgreSQL | `app/tools/postgres.py` | 外部 schema、表范围约束、只读执行和结果流式读取 |
| Sandbox Broker | `app/sandbox_broker.py` | 从私有 Unix socket 接收文件和计划，启动固定分析镜像 |
| 表格 Worker | `sandbox/step7/worker.py` | 解码与预览、表头解析、受限 pandas 分析、Plotly 图表 |
| Caddy | `deploy/Caddyfile` | 可选 HTTPS 部署入口，校验账号并覆盖转发的内部身份头 |

默认 Compose 将 API 和界面绑定到本机回环地址。受控部署由 Caddy 对外接入，API 验证入口密钥后按账号隔离演示库、会话、审批与追踪。挂载的 BIRD 数据库可供配置的账号共同只读访问。部署和状态备份见 [单机部署与恢复](release.md)。

## 数据库问数

1. **确认会话语义。** 待澄清时，短答补入原问题。明确追问只更新用户改变的条件，独立问题重新确定范围。支持的演示营收问题走参数可核对的固定模板。一般问数请求使用模型。
2. **限定 schema。** `SchemaIndex` 基于问题和证据执行 BM25 Top-4，随后补表名命中或字典值命中的候选，并在声明外键图上用 BFS 查找连接路径。可选 BGE-M3 将表和列文档编码到本地 Chroma，以余弦距离分别召回并按表去重。
3. **补充字段含义。** `column_notes.py` 渲染官方说明及值编码，`auto_column_notes.py` 按相关性选择字段，并使用有限 SQLite 样本识别数值文本、日期格式、稀疏列等存储特征。渲染有条数与字符预算。
4. **校验和执行。** 模型按 OpenAI 兼容协议返回结构化 SQL。交互模式还可要求补充条件或报告当前数据源无法回答。Pydantic 校验 SQL 候选后，由对应适配器在只读连接中以 `EXPLAIN` 试跑。PostgreSQL 在试跑前解析 SQL 语法树、限定实际访问的表。正式执行使用独立的只读连接，限制时间与返回规模。
5. **有限恢复。** 对语法或可定位的引用错误，LangGraph 在同一 schema 上修复 SQL，或补入缺失表列及其外键路径后重新生成。修复和补检索共享最多三次重试。遇到重复错误、权限违规、超时或预算耗尽时结束。
6. **返回可检查的结果。** 界面提供 SQL、预览行和完整结果 CSV，固定演示营收查询同时显示日期、地区和毛净口径。离线 EX 用参考结果判断语义是否正确。

演示备注更新有独立审批路径。只有匹配会话及审批 ID 的显式决定可以继续。备注更新与审批去重记录在同一事务内提交，便于在进程中断后恢复。

## 文件分析

`app/file_analysis.py` 先在 worker 中检查文件结构。预览和执行复用工作表索引、表头起始行和表头深度。多表布局有歧义时要求用户选择。表头会压平为唯一列名，合并表头可继承上层分组标签，数据区空白保留为缺失。

`app/file_planner.py` 将自然语言转成受约束的任务，Pydantic 协议限定列索引、筛选操作、分组、排序和结果上限。字段明确的本地任务可以直接生成计划。需要模型规划时，传入问题和 schema 元数据。原始文件由 API 经 Unix socket 传给本机 broker，再经标准输入送进 worker。worker 的执行操作随镜像打包。

- `tasks.py` 先过滤再统计、聚合或排序，表格和图表使用相同的 Top N 结果。
- `audit.py` 执行四则运算表达式，逐行比较原值与重算值。公式、目标单位和输入单位在执行前得到确认。标准换算允许使用可核对的表头推断。
- `panel.py` 处理跨年实体覆盖、共同样本对照和固定样本时序。
- `tabular.py` 在统一布局下读取 CSV、XLSX 和 XLS，限制行列、预览与压缩内容大小。

工作容器禁用网络，根文件系统只读，以非 root 身份运行，限制 CPU、内存、进程数和时长。broker 持有本机 Docker socket，API、界面和 worker 通过各自的接口访问所需资源。

## 追踪与复现

工作流返回 NDJSON 事件。服务以 SQLite 保存 trace ID、步骤时间、失败类别、模型调用次数和 token，用于在界面查看。可选 LangSmith 导出同类追踪元数据。SQL、原始行和文件内容留在业务执行路径中，追踪导出不包含这些字段。

离线评测使用固定的 BIRD Mini-Dev 快照、50 道开发题和 200 道不重叠留出题。另用 250 道未参与检索规则调试的题检查召回。参考 SQL 的表列读集用于 Recall 评分，参考结果用于 EX 评分。模型、提示版本、运行配置和逐题结果写入本地 `evaluation/results/`。

```text
app/                API、界面、会话、检索、SQL 验证和追踪
sandbox/step7/      表格读取、受限执行协议和独立镜像
evaluation/         BIRD、检索和文件分析评测
data/bird/          数据获取说明
deploy/             Caddy 入口配置
docs/               架构、使用、部署与评测复盘
tests/              回归和安全边界测试
```

进一步阅读 [BIRD 数据来源](../data/bird/SOURCES.md)、[完整 schema 基线](v1-baseline.md)、[自动补注与留出验证](v2-holdout-and-automation.md)、[SQL 恢复](v3-step5-dry-run.md) 和 [文件分析](step7-file-analysis.md)。
