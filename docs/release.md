# 单机受控上线

这个部署配置供已知账号的小范围试用。部署方负责选择主机、域名、允许上传的数据范围、模型服务和备份保管人。所有账号可访问同一份已挂载的只读 BIRD 数据。每个账号拥有独立的演示库、会话、审批和追踪。

## 入口与配置

1. 准备专用 Docker 主机和 Compose v2.24.4 或以上版本。分析启动服务需要访问 Docker daemon。尽量使用专用的 rootless daemon，或隔离的单用途主机。把对应的本地 Unix socket 写入 `RAG_DOCKER_SOCKET`。
2. 将 `.env.production.example` 复制为 `.env.production.local` 并限制权限。用 `openssl rand -hex 32` 生成 `RAG_GATEWAY_KEY`。每次发布使用独立的 `RAG_SANDBOX_IMAGE` 标签。
3. 用 `docker run --rm -it caddy:2.11.9-alpine caddy hash-password` 为每个账号生成哈希。每行以空格分隔账号与哈希，写入受保护的本地文件，并将绝对路径填到 `RAG_USERS_FILE`。一个人使用一个账号，移除账号后重新创建入口容器以生效。
4. 本机验收使用示例中的 `localhost` 和高端口。正式部署填写已指向主机的域名，并按主机网络策略设置入口绑定及 80、443 端口。防火墙只对预定访问范围开放入口。
5. 构建需要访问 Docker Hub、PyPI 和 Debian 安全更新源。发布时保留已验收的镜像标识，以便复现和回退。
6. 如需远程模型，填入经批准的 HTTPS 地址和凭据。数据库问答会向该模型提交相关表结构、问题、证据及可能用到的类别线索。文件分析可由用户显式开启远程规划，发送问题和表结构，计算留在分析容器内。

账号文件只存密码哈希。TLS 入口覆写内部身份头，应用验证入口密钥后才读取用户状态。API 和界面不单独发布端口。Streamlit 保持 XSRF 防护。默认最多并行执行四个工作流，单账号最多两个；上传为 10 MiB，结构化 API 请求为 16 KiB。

在项目目录执行下面的命令。

```sh
docker compose --env-file .env.production.local \
  -p rag-prod -f compose.yaml -f compose.production.yaml up -d --build --wait
```

需要只读数据库时增加 `-f compose.bird.yaml`，并在同一环境文件中指定 `RAG_BIRD_DATA_DIR`。入口地址采用相应域名和 HTTPS 端口。网关提供受保护的 `/api/v1/` 接口。本机内部证书仅用于验收。

## 发布前检查

- 使用两个账号验证登录、同一会话 ID 的隔离、审批回放和追踪访问。检查匿名及跨站写请求。
- 用实际部署的模型账户验证一条经批准的合成 SQL 查询，确认额度、延迟和错误处理。LangSmith 如启用，核实目标工作区是否收到脱敏追踪。
- 用小样本完成 CSV 和 Excel 上传。运行公式核对、来源统计及跨年同口径分析，确认错误请求得到明确提示。
- 在目标主机确认工作容器无网络、无宿主目录挂载，且内存、CPU 和进程数限制生效。
- 验证真实域名证书、外部访问范围、备份可恢复和运维告警。监控磁盘、容器重启、HTTP 5xx、模型配额及响应时间，并按数据保留政策清理状态卷。
- 依赖审计、镜像扫描和目标环境容量验收通过后再放开访问。当前方案使用单实例 SQLite，适合受控试用。

## 状态备份与回退

先通过入口停止接收请求，并等正在运行的分析完成。停止网关、界面和 API，然后用服务镜像备份状态卷。下面的示例依赖当前目录的生产配置，备份文件应保存在受保护的存储里。

```sh
set -eu
umask 077
dc() { docker compose --env-file .env.production.local -p rag-prod -f compose.yaml -f compose.production.yaml "$@"; }
dc stop gateway ui api
dc run --rm --no-deps -T --entrypoint tar api -C /state -czf - . > rag-state.tar.gz
dc up -d --wait
```

使用独立 Compose 项目和空状态卷演练恢复。校验备份后，使用目标项目的 `docker compose run --rm --no-deps -T --entrypoint tar api -C /state -xzf - < rag-state.tar.gz` 恢复。然后用测试账号验证会话、审批和追踪。镜像、配置和账号资料需与状态备份配套保管。出现故障时，将已验证的前一版镜像与其对应的备份恢复到独立项目，通过冒烟验证后切换入口。

参考 [Docker Compose 合并规则](https://docs.docker.com/reference/compose-file/merge/)、[Caddy Basic Auth](https://caddyserver.com/docs/caddyfile/directives/basic_auth) 和 [Docker rootless 模式](https://docs.docker.com/engine/security/rootless/)。

## 本机验收记录

- 62 项自动回归测试通过。
- 独立 HTTPS 环境通过匿名和错误密码拦截、伪造用户头拦截、双账号会话与追踪隔离、审批重放及并发限制。
- SQL 流程通过结构检索、缺列修复、写入拦截和用量记录。真实 DeepSeek 合成查询也已通过。
- CSV、XLSX、XLS 读取通过。核对了概况、异常值与绘图、来源统计、公式校验、跨年覆盖和模型故障提示。浏览器通过 HTTPS 登录、WebSocket、XSRF 上传和绘图。
- 合成状态卷已成功备份并恢复，SQLite 完整性、审批记录和私有追踪均已核对。
- 工作容器验证了非 root、根文件系统只读、无外网路由和无 Docker socket。两套镜像的 Python 运行依赖公告扫描未报已知漏洞。

## 镜像扫描结论

系统包扫描发现旧 Bookworm 镜像的 SQLite 告警，API、界面和文件分析已重建在 Debian Trixie 上，并安装了安全源中的 zlib 补丁。使用 Trivy 复扫后，Caddy 系统包无高危或严重告警。Python 容器的共同 Debian 底座仍报告 47 项高危、0 项严重告警，扫描时这些高危项均无标注的可安装修复版本。四个镜像重复继承同一批系统包，数量按单镜像计算。

其中 SQLite 的剩余公告涉及写入 FTS5、带有破损索引的删除或 changeset 应用；生产入口接收 CSV/Excel，SQL 查询受只读约束，数据库由部署方提供。这些限制缩小了可达范围。对外开放前，仍需在目标主机复扫和核查剩余告警，确认风险处置、真实域名证书和预计流量下的容量。

参考 [Debian SQLite 旧版公告](https://security-tracker.debian.org/tracker/CVE-2025-7458)、[Debian zlib 安全公告](https://security-tracker.debian.org/tracker/CVE-2026-76844) 和 [Trivy](https://github.com/aquasecurity/trivy)。
