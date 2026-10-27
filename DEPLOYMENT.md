# 部署与威胁模型

面向自托管的网络部署：把 scrape-mcp 作为常驻 HTTP 服务跑在容器里，暴露给可信的 MCP 客户端或网关。
本机 stdio 用法见 [README](README.md)，这里只讲网络部署。

## 1. 部署形态

| 形态 | 适用场景 | 边界 |
|---|---|---|
| 本机 stdio | 单机 Agent 客户端直接拉起进程 | 进程边界即安全边界 |
| 容器 + 回环端口 | 同机的反向代理或网关转发 | Bearer 令牌 |
| 容器 + 公网反向代理 | 多客户端共享一个抓取出口 | Bearer 令牌 + TLS + 代理层限流 |

硬性前提：**绑定非回环地址时必须配置至少一个访问令牌**（`SCRAPE_MCP_HTTP_TOKEN`，或多令牌的 `SCRAPE_MCP_HTTP_TOKENS` / `SCRAPE_MCP_HTTP_TOKENS_FILE`），否则进程直接退出，不会以无鉴权状态启动。

## 2. 用 Docker Compose 启动

```bash
cp .env.example .env
# 在 .env 里写入至少一项：SCRAPE_MCP_HTTP_TOKEN=<足够长的随机串>
# 需要每个调用方独立配额时改用 SCRAPE_MCP_HTTP_TOKENS（JSON）或 SCRAPE_MCP_HTTP_TOKENS_FILE
docker compose up -d --build
curl http://127.0.0.1:8002/healthz
```

`docker-compose.yml` 的默认行为：

- 端口只映射到 `127.0.0.1`，公网入口交给反向代理；需要局域网直连时改成 `"8002:8002"`
- 数据卷 `scrape_data` 挂到 `/data`，保存登录态与两层缓存，重建容器不丢
- `no-new-privileges`，镜像内以 uid/gid `10001` 运行
- 需要 L2 真浏览器时：`INSTALL_BROWSER=1 docker compose up -d --build`（Chromium 会显著增大镜像）
- 构建阶段需要从 PyPI 拉依赖；受限网络下用 `PIP_INDEX_URL=https://<镜像源>/simple docker compose up -d --build` 换源

客户端接入地址：`http://127.0.0.1:8002/mcp`（streamable-http），请求头带 `Authorization: Bearer <令牌>`。

## 3. 直接运行镜像

```bash
docker run -d --name scrape-mcp \
  -p 127.0.0.1:8002:8002 \
  -e SCRAPE_MCP_HTTP_TOKEN=<令牌> \
  -v scrape_data:/data \
  --security-opt no-new-privileges \
  ghcr.io/guanyuyan/scrape-mcp:latest
```

镜像内置的环境：`SCRAPE_MCP_TRANSPORT=streamable-http`、`SCRAPE_MCP_HTTP_HOST=0.0.0.0`、
`SCRAPE_MCP_HTTP_PORT=8002`、`SCRAPE_MCP_DATA_DIR=/data`。入口命令为
`scrape-mcp --transport streamable-http --port 8002`。

镜像已内置 PDF 文本抽取（`pypdf`），`web_fetch` 在容器里可直接读 PDF；L2 浏览器层按需通过
`INSTALL_BROWSER=1` 加上，不是所有页面的必需项。

多租户时把 `-e SCRAPE_MCP_HTTP_TOKEN=<令牌>` 换成单行 JSON 的 `-e SCRAPE_MCP_HTTP_TOKENS='[...]'`，
或用 `-v` 挂一个令牌文件并设 `SCRAPE_MCP_HTTP_TOKENS_FILE=/data/tokens.json`（三者互斥）。

## 4. 反向代理与 TLS

Caddy 最小配置（自动申请证书）：

```caddyfile
scrape.example.com {
    reverse_proxy 127.0.0.1:8002
}
```

- TLS 在代理层终止，应用层仍要求 Bearer 令牌，两层都保留
- `/healthz` 免鉴权且会返回版本号；不希望暴露时在代理层限制该路径的来源
- 默认 `json_response=true`，普通反向代理即可；若改成事件流模式，需要关闭代理缓冲

## 5. 关键配置

| 变量 | 建议 |
|---|---|
| `SCRAPE_MCP_HTTP_TOKEN` | 单令牌模式（三选一）。用 `openssl rand -hex 32` 生成；轮换后重启容器即可 |
| `SCRAPE_MCP_HTTP_TOKENS` | 多租户模式（三选一）。单行 JSON：`[{"name":"team-a","token":"...","qps":2,"daily_quota":2000}]`；租户名与令牌都必须唯一 |
| `SCRAPE_MCP_HTTP_TOKENS_FILE` | 多租户模式（三选一）。同格式 JSON 文件路径，建议放 `/data/tokens.json` 随卷保管 |
| `SCRAPE_MCP_HTTP_QPS` | 每租户每秒请求上限，默认 5；多令牌时作为省略 `qps` 的租户默认值 |
| `SCRAPE_MCP_HTTP_DAILY_QUOTA` | 每租户每日请求配额，默认 10000（UTC 零点清零）；多令牌时作为省略 `daily_quota` 的租户默认值；`0` 不限 |
| `SCRAPE_MCP_MAX_QPS` | 全网抓取限流，默认 10 次/秒；比 HTTP 层限流更贴近目标站压力 |
| `SCRAPE_MCP_ALLOWED_HOSTS` | 目标站白名单（逗号分隔）。公网部署建议设置，避免被当成任意 URL 抓取器 |
| `SCRAPE_MCP_DENIED_HOSTS` | 目标站黑名单，优先于白名单；可用来挡内网网段域名 |
| `SCRAPE_MCP_PROXY_POOL` | 出口代理池（逗号/分号/空白分隔）；比单个 `SCRAPE_MCP_PROXY` 优先 |
| `SCRAPE_MCP_CACHE_TTL` | 两层缓存 TTL 秒数，默认 300；`0` 全关 |
| `SCRAPE_MCP_DATA_DIR` | 登录态与缓存目录，镜像内固定为 `/data`，挂卷持久化 |
| `SCRAPE_MCP_L2_ENABLED` | 是否允许升级真浏览器；不装浏览器层时应设为 `false` |

完整变量清单见 [README 环境变量](README.md#环境变量前缀-scrape_mcp_) 与 `.env.example`。

## 6. 健康检查与观测

- `GET /healthz`：免鉴权，返回 `version` / `l2_enabled` / `respect_robots`，不触发任何抓取
- `GET /stats`：进程内计数（请求数、分层、错误码、缓存命中、延迟分位）。配置令牌后需 Bearer；未配置时只允许回环访问
- `GET /metrics`：Prometheus 文本格式（`version=0.0.4`），访问限制与 `/stats` 相同。抓取量按层级/错误码/内容形态分列，另有鉴权与限流拒绝计数、L2 浏览器占用
- 日志：JSON 行写 stderr，`docker logs -f scrape-mcp` 直接可读；`SCRAPE_MCP_LOG_LEVEL=INFO` 可看到每次抓取的分层与耗时

Prometheus 抓取（容器与 Prometheus 同网络时用服务名；否则写宿主地址）：

```yaml
scrape_configs:
  - job_name: scrape-mcp
    metrics_path: /metrics
    authorization:
      type: Bearer
      credentials: "<SCRAPE_MCP_HTTP_TOKEN>"
    static_configs:
      - targets: ["scrape-mcp:8002"]
```

建议关注：`rate(scrape_mcp_fetches_total[5m])`（抓取量）、`sum by (tier) (scrape_mcp_fetch_tier_total)`（L1/L2 命中层级）、`rate(scrape_mcp_http_rejections_total[5m])`（被 401/429 拒绝的调用）、`scrape_mcp_l2_active_pages` / `scrape_mcp_l2_contexts`（浏览器占用）、`sum by (tenant) (rate(scrape_mcp_tenant_requests_total[5m]))`（各租户用量）与 `sum by (tenant, reason) (rate(scrape_mcp_tenant_rejections_total[5m]))`（哪个租户被限流/超配额）。`/metrics` 不计入 QPS 与日配额（鉴权仍生效），可按 15s~60s 间隔稳定抓取。

## 7. 容量与资源

- 并发上限由三层共同约束：HTTP 层 `HTTP_QPS`、抓取层 `MAX_QPS`、同域 `HOST_MAX_CONCURRENCY`（默认 2）
- 批量与爬取另有限额：`BATCH_MAX_CONCURRENCY`（4）、`BATCH_MAX_URLS`（20）、`CRAWL_MAX_PAGES`（20）
- L2 同时打开的页面上限 `L2_MAX_PAGES`（3），常驻 context 上限 `L2_MAX_CONTEXTS`（8）
- 内存：仅 L1 时 512MB 起步即可；启用 L2 建议 2GB 起步
- 磁盘：缓存按响应体大小落盘，长期运行注意 `/data` 占用（可调小 `CACHE_TTL` 或定期清理卷）

## 8. 升级与回滚

- 生产环境固定 tag（如 `0.10.0` 或 `sha-<commit>`），不要用 `latest`
- 升级：`docker compose pull && docker compose up -d`；回滚：把 tag 改回去再 `up -d`
- 删除数据卷会同时丢掉登录态与缓存；升级镜像本身不影响卷

## 9. 威胁模型

### 资产

1. HTTP 访问令牌
2. 目标站登录态（cookie / localStorage，落在 `/data/sessions/`）
3. 代理凭据（`SCRAPE_MCP_PROXY` / `SCRAPE_MCP_PROXY_POOL`）
4. 抓取到的内容与缓存
5. 宿主资源与出网带宽

### 信任边界

- **客户端 ↔ 服务**：不可信网络，可能被扫描、爆破、重放
- **服务 ↔ 目标站**：目标站不可信，会返回任意 HTML/JS/PDF，也可能反查来源
- **容器 ↔ 宿主**：容器内进程不应获得宿主权限
- **运维 ↔ 配置**：环境变量与 `.env` 里存放令牌与代理凭据

### 威胁与缓解

| 威胁 | 影响 | 缓解 |
|---|---|---|
| 无鉴权暴露或令牌被爆破 | 服务被白嫖、目标站被大量抓取 | 非回环绑定强制令牌；高熵令牌；`HTTP_QPS` + `HTTP_DAILY_QUOTA`；多租户时每令牌独立配额，一个租户失陷不影响其它租户额度；反向代理层再加限流 |
| 令牌进入日志或 URL | 凭证泄露 | 令牌只从请求头读取、不写日志；日志只记 URL/分层/错误码；轮换只需换环境变量并重启 |
| 批量/爬取打满资源 | 容器 OOM、宿主受影响 | 批次与页面数上限、同域并发上限、`deadline_ms` 预算、响应体截断、L2 页面上限 |
| 登录态泄露 | 目标站账号被滥用 | 卷仅容器用户可写；镜像与仓库都不含登录态；按 `session` 名隔离多身份；不用时删除对应文件 |
| 代理凭据泄露 | 代理额度被刷 | 只从环境变量读取；不写日志；工具返回的载荷里不包含代理地址 |
| 被当成任意 URL 抓取器（SSRF） | 探测容器可达的内网服务 | 公网部署设置 `ALLOWED_HOSTS` 白名单，或在编排层做出口网络策略；不要把本服务当开放代理 |
| 恶意页面拖垮解析 | CPU/内存被畸形 HTML/PDF 占满 | 响应体上限、PDF 页数上限、正文抽取只看文本；L2 依赖的 Chromium 及时随基础镜像更新 |
| 容器逃逸或提权 | 宿主失守 | 非 root（10001）+ `no-new-privileges`；可进一步只读根文件系统 + `tmpfs: /tmp`；绝不挂载 `docker.sock` |
| 供应链投毒 | 恶意代码进入运行环境 | 依赖来自 PyPI 且带版本区间；镜像在 CI 构建并推 GHCR；生产固定 tag/digest，升级前看 CHANGELOG |

### 明确不提供的保证

- **多租户只隔离配额，不隔离资源**：多令牌模式下每个令牌有独立的 QPS 与日配额，但所有调用方仍共享同一出口 IP、同一份缓存与登录态，抓取层 `MAX_QPS` 与 L2 浏览器容量也是全局的——某个租户的批量任务会占用公共抓取预算。需要彻底隔离时按租户各起一套实例
- **不做内容安全审核**：返回的是目标站原文，是否可信由调用方判断
- **不保证绕过所有反爬**：拿不到正文时如实返回 `blocked` / `login_required`，不会把挑战页伪装成内容
- **不隐藏客户端身份**：容器出口 IP 即所有请求的出口，需要分散时用代理池

## 10. 故障排查

| 现象 | 排查方向 |
|---|---|
| 容器启动即退出 | 是否设置了 `SCRAPE_MCP_HTTP_TOKEN` / `HTTP_TOKENS` / `HTTP_TOKENS_FILE` 之一；多令牌 JSON 是否单行且合法（租户名/令牌重复、缺字段都会拒绝启动）；`docker logs` 里有明确的拒绝原因 |
| `healthz` 不通 | 容器内端口是否 8002；映射端口是否被占用；先用 `docker exec scrape-mcp python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8002/healthz').status)"` 自检 |
| 持续 401 | 请求头是否为 `Authorization: Bearer <令牌>`；令牌是否带多余空白；反向代理是否剥掉了该头 |
| 持续 429 | 命中 `HTTP_QPS` 或日配额，按响应里的 `Retry-After` 退避；`/healthz` 与 `/metrics` 不消耗配额；多租户时看 `/metrics` 的 `scrape_mcp_tenant_rejections_total` 确认是哪个租户、被哪条规则拦下 |
| 全部抓取失败 | 容器出网/DNS/代理；在容器内请求一个已知站点验证；检查代理池是否整池冷却 |
| 登录态失效 / 需要登录的站 | 容器内没有图形界面，无法手动登录。① 本机已 `login` 过：`docker exec scrape-mcp mkdir -p /data/sessions` 后 `docker cp <宿主>/.scrape_mcp/sessions/<host>.json scrape-mcp:/data/sessions/<host>.json`（保留 localStorage）；② 只有 cookie：`docker cp cookies.txt scrape-mcp:/data/cookies.txt` 再 `docker exec scrape-mcp scrape-mcp session import --url https://<站> --file /data/cookies.txt`，支持 JSON 与 Netscape `cookies.txt` |
| PDF 抓取报缺依赖 | 镜像已内置 `pypdf`；自己改装镜像时用 `pip install "mcp-scrape[pdf]"` |
