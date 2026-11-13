# 只读 PostgreSQL 数据源

配置连接器后，在 Streamlit 的「数据源」选择「PostgreSQL」。侧栏可查看表、列类型、注释和外键，并可为选中的浏览表请求少量示例值。选择一张或多张表后，当前查询和追问都使用该范围。未指定表时，可查询部署方开放的整个 schema。会话只复用同一数据源和同一表范围的追问与澄清状态。

## 启动十表示例库

已有模型配置的项目可在根目录运行下面的命令。PostgreSQL 管理员和只读账号使用独立密码。初始化从本地合成数据创建零售 schema，重复运行会补齐记录并保留已有记录。

```bash
if [ ! -e .env.postgres.local ]; then
(umask 077
 printf 'RAG_POSTGRES_ADMIN_PASSWORD=%s\nRAG_POSTGRES_PASSWORD=%s\n' \
   "$(openssl rand -hex 24)" "$(openssl rand -hex 24)" > .env.postgres.local)
fi
docker compose --env-file .env --env-file .env.postgres.local \
  -p rag-postgres-demo -f compose.yaml -f compose.postgres.yaml up -d --build --wait
```

保留同一份 `.env.postgres.local` 以便重启已有数据库卷。项目同时运行时，可用 `RAG_API_PORT` 和 `RAG_UI_PORT` 指定未占用的本地端口。示例库在 Compose 私有网络中，宿主机只访问界面和产品 API。

例如选中 `regions`、`customers`、`orders`、`payments` 和 `refunds` 后，可以问「按客户所在地区计算 2026 年 10 月下单订单的净收入」。这道题涉及拆分付款和同笔付款的多次退款，先汇总每笔付款的退款再关联订单可以避免重复扣款。

## 配置实际数据源

用基础 Compose 配置连接外部 PostgreSQL。在部署环境或应用密钥中设置 `RAG_POSTGRES_HOST`、`RAG_POSTGRES_PORT`、`RAG_POSTGRES_DATABASE`、`RAG_POSTGRES_USER`、`RAG_POSTGRES_PASSWORD` 和 `RAG_POSTGRES_SCHEMA`。生产模式要求 TLS `verify-full`，连接地址须匹配数据库证书。使用私有 CA 时，将证书只读挂载至 API 容器，并用 `RAG_POSTGRES_SSLROOTCERT` 指向容器内路径。[PostgreSQL TLS 校验](https://www.postgresql.org/docs/17/libpq-ssl.html)

为应用分配独立登录角色，对单个受信任 schema 授予 `USAGE`，再对拟公开的表授予 `SELECT`。读者角色应无管理权限、表写权限和 schema 创建权限。连接器仅列出符合检查条件的普通表。分区、视图、外部表、行级安全表和使用自定义类型的表不在当前支持范围。单个 schema 最多 100 张表，每表最多 100 列。

在目标库以该角色直连，核对 `session_user`、`current_user`，并查询 `pg_stat_ssl` 中 `pid = pg_backend_pid()` 的加密状态。检查读者账号的对象所有权和角色成员关系，包括通过 `SET ROLE` 可取得的权限。对验收用表在显式 `BEGIN READ WRITE` 下尝试写入、创建对象和切换管理员角色，确认服务器拒绝这些操作。[PostgreSQL 角色成员关系](https://www.postgresql.org/docs/17/role-membership.html)

`GET /api/v1/databases` 返回可用来源。用 `GET /api/v1/databases/postgres/schema` 获取结构。示例值需显式指定 `?table=orders&include_samples=true`，每列最多展示 3 个截断后的值。向 `POST /api/v1/query` 或 `POST /api/v1/chat` 提交 `database: "postgres"`，可用 `tables` 数组限定查询范围。连接配置只由部署环境注入。

## 查询机理和边界

适配器读取 PostgreSQL catalog，将表、列、注释和外键交给现有 BM25 和有界恢复。生成 SQL 经语法树检查，只允许一个受限的只读 SELECT。模型可用的函数来自批准的 PostgreSQL 内置函数，表引用必须属于选中的范围。执行使用独立读者账号、只读事务、5 秒执行期限和服务端游标。产品沿用 50 行预览、最多计数 100,000 行和 8 MiB CSV 快照限制。数值列在需要保持 PostgreSQL 十进制精度时以字符串返回。

模型接收问题、证据、表结构、注释和关联信息。浏览面板请求的示例值不加入模型提示。例库 SQL 和返回的示例结果由合成数据生成。按部署账号的数据授权范围开放表和列，已有入口认证与结果归属继续生效。[PostgreSQL schema 文档](https://www.postgresql.org/docs/17/ddl-schemas.html) · [Psycopg 游标文档](https://www.psycopg.org/psycopg3/docs/advanced/cursors.html)

## 验收

```bash
python -m evaluation.run_postgres_demo --base-url http://127.0.0.1:8000
```

脚本通过产品 API 检查十个业务问题的有序结果、选表范围、CSV 和追踪。它还检查 schema 浏览、示例值和非法选表。CI 的 `--replay` 模式用固定模型回复执行同一批真实 PostgreSQL 查询，另测跨表越权、写入拒绝、超时和大结果截断。
