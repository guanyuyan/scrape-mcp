# Changelog

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。所有显著变更都会记录在本文件。

## [Unreleased]

### Changed
- 裸 403 不再提示登录：`waf` 类拦截（401/403/451/5xx）只有页面确实出现登录入口（密码框或「登录/密码/sign in」字样）才报 `login_required`，否则按 `blocked` 如实上报。此前澎湃首页这类出口/IP 风控的拒绝页会附上「调用 login 手动登录」，对用户是误导；Stack Overflow 的期望值随之放宽为 `["blocked", "login_required"]`
- 基准的 `expect` 支持写成列表：同一站在不同出口结果不同时（如澎湃首页在 GitHub Actions 出口被 WAF 恒定 403、在住宅出口正常取正文），列表内任一结果都算符合预期
- `thepaper-home` 期望值改为 `["ok", "blocked", "login_required"]`。三次全量 CI 工件里它都是 `status=403`、`blocked=waf`、0 字，而同站 `thepaper-article` 稳定取到 2827 字——是首页路径对数据中心出口的 WAF 拦截，此前被记作「间歇性 `login_required`」；全量回归回到 37/37

## [0.14.1] - 2026-10-28

### Added
- **真实站点基准扩容**：`benchmarks/sites.json` 从 24 站扩到 37 站，新增 Python `json`、Django 教程、Rust 所有权章节、博客园文章、美团技术团队、澎湃固定文章、网易 / 新浪首页、豆瓣话题、Svelte / Tailwind 首页、淘宝首页与第二份文本层 PDF；夜间回归子集同步增加到 14 站
- 夜间工作流新增 `scope` 输入（`nightly` / `all`），可在 GitHub 网络上按需跑全量站点集，扩容后先用它校验期望值再依赖夜间子集

### Changed
- 夜间任务超时从 35 分钟放宽到 45 分钟，给扩容后的全量跑留出余量
- `benchmarks/baseline.json` 随扩容重录：夜间子集 14 站，14/14 通过，p50 504 ms / p95 1642 ms
- Stack Overflow 的期望值从 `ok` 改为 `login_required`：该站长期拦截未登录抓取，作为「如实上报拦截」的回归项
- **L2 渲染等待收敛**：settle 阶段判定「还在等骨架页」的可见文本下限，从 L1 口径（2000 字）降到 1000 字。浏览器已经把 JS 跑过一遍，此时仍有上千字可见文本的页面正文早已稳定，继续按 2000 字判定只会空转到 `shell` 的 4 秒封顶——澎湃首页（渲染后 1431 字）与 Svelte 首页（1943 字）实测每站白等 3 秒以上。`L1 → L2` 的升级判定不变（`apple.com.cn` 这类确实需要渲染的站仍会升级）

## [0.14.0] - 2026-10-28

### Added
- **同 URL 单飞（singleflight）**：同一 URL 的并发抓取只真正执行一次，其余等待并复用同一结果。L1 共享缓存路径按「指纹 + URL」合并，L2 渲染按「URL + 会话 + 交互参数 + 预算」合并；多租户同抓同一页面、批量/爬取里的重复链接不再成倍占用出口与浏览器页位
- 带自定义头/凭证或 `bypass_cache=true` 的请求可能因人而异，不参与合并；领头请求被取消时等待者自动接棒，不会跟着失败

## [0.13.0] - 2026-10-28

### Added
- **耗时直方图**：`scrape_mcp_fetch_duration_seconds`（按 `tier` 分桶 `0.1`/`0.3`/`1`/`3`/`10`/`+Inf` 秒），可直接用 `histogram_quantile` 算分层 p95，不再需要读 `/stats` 的窗口分位数
- **租户配额余量**：`scrape_mcp_tenant_quota_remaining{tenant}`，只输出设置了 `daily_quota` 的租户，可用于余量告警
- **代理池健康度**：`scrape_mcp_proxy_pool_size` / `_healthy` / `_cooling`；未配置 `PROXY_POOL` 时不输出这组指标
- **L2 排队数**：`scrape_mcp_l2_queued_pages`，等待渲染页位的请求数，与在途页数一起看容量是否吃紧

## [0.12.0] - 2026-10-27

### Added
- **多令牌与租户级配额**：新增 `SCRAPE_MCP_HTTP_TOKENS`（JSON）与 `SCRAPE_MCP_HTTP_TOKENS_FILE`（同格式文件），一次配置多个调用方令牌；每项 `{"name","token","qps","daily_quota"}` 独立限流与日配额，`qps` / `daily_quota` 省略时继承全局 `HTTP_QPS` / `HTTP_DAILY_QUOTA`，写 `0` 表示该租户不限
- **租户指标**：`/metrics` 新增 `scrape_mcp_tenant_requests_total{tenant}` 与 `scrape_mcp_tenant_rejections_total{tenant,reason}`，按令牌区分用量与被限流原因
- **配置校验**：租户名与令牌必须唯一；单令牌与多令牌（环境变量 / 文件）三者互斥，JSON 非法或缺字段时启动即报错退出，不会静默降级为无鉴权

### Changed
- `HTTP_QPS` / `HTTP_DAILY_QUOTA` 由「单令牌」表述改为每租户默认值；单令牌模式行为不变（租户名 `default`）
- `/metrics` 继续跳过 QPS 与日配额（鉴权仍生效），Prometheus 固定间隔抓取不会占用任何租户额度

## [0.11.0] - 2026-10-27

### Added
- **Prometheus `/metrics` 端点**：输出文本格式（`version=0.0.4`），与 `/stats` 同一访问限制（配置令牌后需 Bearer，未配置时仅回环）。含抓取量（`scrape_mcp_fetches_total` / `scrape_mcp_requests_total`）、命中层级（`scrape_mcp_fetch_tier_total{tier}`）、错误码与内容形态分布、两层缓存命中、token/字节累计
- **拒绝计数**：`scrape_mcp_http_rejections_total{reason}` 按 `unauthorized` / `forbidden` / `rate_limited` / `quota_exceeded` 记录鉴权与限流拒绝
- **L2 占用指标**：`scrape_mcp_l2_browser_running` / `scrape_mcp_l2_active_pages` / `scrape_mcp_l2_contexts` 与 `l2_max_pages` / `l2_max_contexts` 上限，浏览器未启动时占用为 0、上限照配置输出
- **监控不计配额**：`/metrics` 与 `/healthz` 一样跳过 QPS 与日配额（鉴权仍生效），Prometheus 按固定间隔抓取不会吃掉调用额度

不引入 `prometheus_client`，按文本格式直接拼装；指标不含 URL 与页面内容。

## [0.10.1] - 2026-10-23

### Added
- **`scrape-mcp session` 子命令**：`session list` 查看已保存的登录态，`session import` 直接从 cookie 文件导入（不需要浏览器），容器/无显示环境下也能补登录态
- **Netscape `cookies.txt` 支持**：`session_import` 工具与 CLI 共用同一套解析，兼容浏览器扩展导出格式、`#HttpOnly_` 前缀、secure 与过期时间

### Fixed
- **容器内 PDF 抓取**：镜像改为安装 `mcp-scrape[pdf]`；此前容器里 `web_fetch` 遇到 PDF 会因缺 `pypdf` 失败

## [0.10.0] - 2026-10-23

### Added
- **HTTP Bearer 鉴权**：设置 `SCRAPE_MCP_HTTP_TOKEN` 后，除 `/healthz` 外所有 HTTP 请求必须携带 `Authorization: Bearer <token>`；缺失或错误统一返回 401（带 `WWW-Authenticate`），响应不含内部路径与版本
- **启动期保护**：HTTP 绑定非回环地址（如 `0.0.0.0`）但未配置令牌时拒绝启动；未配置令牌时仍可在 `127.0.0.1` / `localhost` / `::1` 上零配置使用
- **单令牌配额**：`SCRAPE_MCP_HTTP_QPS`（默认 5 次/秒，令牌桶）与 `SCRAPE_MCP_HTTP_DAILY_QUOTA`（默认每日 10000 次，UTC 零点清零）超限返回 429，带 `Retry-After` 头与 `retry_after_s` 字段（`rate_limited` / `quota_exceeded`），客户端可按建议退避；`/healthz` 与未配置令牌的本机模式不受限
- **单请求代理**：`web_fetch` / `web_extract` / `web_batch` / `web_crawl` 支持 `proxy` 参数（`http` / `https` / `socks4` / `socks5`），只对本次调用生效，L1 与升级后的 L2 走同一个出口；不传则沿用 `SCRAPE_MCP_PROXY`。带代理的请求不读也不写共享缓存，L2 context 按代理隔离，避免不同出口的内容与登录态混用
- **代理池**：新增 `SCRAPE_MCP_PROXY_POOL`（逗号/分号/空白分隔，非空时优先于 `SCRAPE_MCP_PROXY`）——出口按轮询分配，同一主机在 `SCRAPE_MCP_PROXY_POOL_STICKY_TTL`（默认 300 秒）内固定走同一个 IP；同一出口连续失败（传输异常，或整条链路走完仍被限流/风控拦截）达 `SCRAPE_MCP_PROXY_POOL_FAIL_THRESHOLD`（默认 2 次）后进入 `SCRAPE_MCP_PROXY_POOL_COOLDOWN`（默认 60 秒）冷却，冷却期内不再分配，整池冷却时回退到最早解冻的出口。单请求 `proxy` 优先级最高且不计入池的失败计数；池化出口属基础设施，结果仍可共享缓存
- **容器镜像**：`Dockerfile` 改为多阶段构建（构建阶段出 wheel，运行镜像不带编译工具），运行阶段以非 root（uid/gid 10001）启动；`INSTALL_BROWSER=1` 可选安装 Chromium，`/data` 卷持久化登录态与两层缓存
- **GHCR 发布**：新增 `.github/workflows/docker.yml`——`main` 推 `latest` 与 sha 标签，`v*` 标签推语义化版本，PR 只构建不推送，使用内置 `GITHUB_TOKEN`
- **`docker-compose.yml`**：开箱可跑；端口默认只映射回环、令牌必填、`no-new-privileges`、独立数据卷，代理与配额通过环境变量透传
- **部署文档与威胁模型**：新增 `DEPLOYMENT.md`（部署形态、反向代理、容量规划、升级回滚、资产与信任边界、威胁缓解、故障排查）

### Changed
- `/stats` 在配置令牌后由 Bearer 鉴权保护，可远程读取；未配置时保持仅回环访问
- HTTP 传输改为显式构建 StreamableHTTP 应用并包上鉴权中间件；`/healthz` 保持免鉴权以支持容器探针

## [0.9.0] - 2026-10-21

### Added
- **真实站点质量基准**：`benchmarks/sites.json` 定义 24 个站点、7 个分层（文档 / 博客 / 资讯 / 列表 / 前端渲染 / 登录墙与反爬 / PDF），每站可配置最小字符数、正文必含片段（`checks`）与期望结果；`expect` 为错误码时表示该站应被如实拦截（登录墙 / robots），而不是伪装成功
- **`scripts/bench_sites.py`**：输出通过率、取正文率、拦截符合率、正文完整度、token、p50/p95 延迟与分层分布；支持 `--category` / `--only` / `--nightly` 过滤，`--json` / `--md` 留档，`--baseline` 对比上次结果并标记正文字符下降超过 20% 的退化，`--reference trafilatura` 可选对照第三方正文抽取器（需自行安装）
- **夜间质量回归**：夜间工作流在演示冒烟之外跑分层站点子集，结果以 `site-quality-<run_id>` 工件留档；通过率低于 60% 时自动开 issue，与"整条链路失败"分开归因

### Changed
- 夜间工作流的演示与质量基准改为独立上报：任一环节失败都会各自开 issue，不再因为演示失败而跳过质量数据采集

## [0.8.0] - 2026-10-21

### Added
- **粘性升级**：L2 真拿到正文后按主机记 10 分钟的"必须上浏览器"，期间同站直接走 L2（`skip_l1:sticky_l2`），省掉每次注定被拦的 L1；TTL 到期、浏览器渲染失败或 URL 明显是静态资源（PDF/图片等）时回到 L1，结论表有上限（512 个主机）
- **更多站点 recipe 示例**：`examples/recipes/` 由 3 份扩到 8 份，新增 MDN 文档、FastAPI 文档、Vue 中文文档（三种常见文档站主题各一份正文根写法）、博客园首页（列表页一次抽 20 条）与中国新闻网文章页（正文 + 时间/来源/作者）；每份都配了实测过的选择器与 `note`
- **`/stats` 耗时分布**：新增 `latency_ms`——最近 1024 次抓取的 p50/p95/p99、固定分桶（`lt_100` … `gte_10000`）与按 L1/L2 分层的 p50/p95。与 payload 的 `elapsed_ms` 同口径，不含结果缓存命中；样本有上限，长时间运行不涨内存

## [0.7.0] - 2026-10-21

### Added
- **正文与抽取结果缓存**：`web_fetch` / `web_extract` 在 HTTP 缓存之外再加一层结果缓存（`<DATA_DIR>/results/`），命中后网络、渲染、解析全部跳过，只读缓存返回；`bypass_cache=true` 同时绕过两层，`debug=true` 与自定义 `headers`/`cookies` 不入缓存。两层共用 `CACHE_TTL`（默认 300 秒，`0` 全关）
- **基准套件**：`scripts/bench.py` 输出冷/热 p50/p95、加速比与 token 数，`--l2` 额外测量浏览器冷启动与复用；README 增加可复现的实测表
- **L1 连接复用**：同一指纹模板在进程内共享 `curl_cffi` 会话与连接池，省掉每次请求重复的 TLS 握手与 DNS 解析；换事件循环自动重建，`aclose()` 统一释放
- **`/stats` 端点**：HTTP 模式下 `GET /stats` 返回进程内计数（总请求、分层/错误码/内容形态分布、两层缓存命中、返回 token 与入站字节），只接受回环地址访问；只记计数不记 URL 与页面内容
- **L2 浏览器池与空闲回收**：同站同会话复用 context（各带独立登录态），超过 `L2_MAX_CONTEXTS` 按最久未用淘汰；`L2_MAX_PAGES` 限制同时打开的页面数（超出排队）；空闲超过 `L2_IDLE_TIMEOUT` 秒自动关掉浏览器进程，下次用到再启动。在途渲染与手动登录期间不会被回收
- **`deadline_ms` 总预算与降级**：`web_fetch` / `web_extract` / `web_batch` / `web_crawl` 支持整次调用的毫秒预算。预算不足时按"压缩单步超时 → 跳过重试 → 不升级 L2 → 直接返回 `timeout`"逐级降级，已拿到的结果照常返回；`web_batch` / `web_crawl` 的队列共用同一预算，未开始的子任务立即返回；合规检查（限流等待、robots.txt）也计入预算，不够时提前返回 `timeout`；降级结果不写入缓存

## [0.6.0] - 2026-10-21

### Added
- **`scrape-mcp init`**：一行生成 Claude Desktop / Cursor / VS Code / 通用 JSON 配置；默认只打印 diff 预览，`--write` 落盘，已有同名条目需 `--force` 才覆盖，`--env KEY=VALUE` 可写入环境变量
- **`scrape-mcp schema`**：导出工具定义（名称、说明、入参 schema），支持 `--tool` 过滤与 `--output` 写文件
- **Python SDK**：`scrape_mcp.sdk` 提供 `fetch` / `extract` / `crawl` / `batch`，与 MCP 工具共用实现，返回解析后的 dict；`aclose()` 可显式释放浏览器
- **示例脚本**：`examples/` 新增 5 个场景脚本（长文正文、列表抽取、翻页合并、PDF 转文本、批量巡检）
- **进度通知**：`web_batch` / `web_crawl` 在客户端请求进度时逐条/逐页上报（并发批量为 `已抓取 n/N 个 URL`，分页为 `已翻到第 n/N 页`）；客户端未请求时零额外开销
- **英文 README**：新增 `README.en.md`，内容与中文版完整对照，顶部提供 `简体中文 | English` 双向切换

## [0.5.0] - 2026-10-21

### Added
- **PDF 文本抽取（可选能力）**：`pip install "mcp-scrape[pdf]"` 后，`web_fetch` 直接返回 PDF 文本（`kind="pdf"`，带 `pages` / `pages_parsed`）；按页兜底，扫描件无文字层时返回 `empty`，未装 extra 时返回 `unsupported_content` 并给出安装提示
- **纯文本直读**：`text/plain`、`text/markdown` 响应按正文返回（`kind="text"`），不再按 HTML 解析
- **命名会话**：`login` / `web_fetch` / `web_extract` / `web_batch` / `web_crawl` 新增 `session` 参数，默认会话沿用旧路径，命名会话存于 `sessions/<session>/<host>.json`，多身份互不覆盖
- **`session_list` / `session_export` / `session_import`**：列出本地登录态、导出 cookie 到返回体或本地文件、从本地 JSON 导入（兼容 `storage_state` / 裸 cookie 数组；默认按 name+domain+path 合并，`replace=true` 整体替换）
- **正文质量**：代码块按 `language-*` / `lang-*` / `highlight-*` / `brush: xxx` 输出语言围栏；表格按 `rowspan`/`colspan` 展开成对齐 CSV；`figure` 输出 `![alt](绝对地址)` 与 `figcaption` 图注
- `unsupported_content` 错误码（不可重试），用于"内容形态不支持该工具"的场景
- `PDF_MAX_PAGES` 配置项（默认 200）
- **站点 recipe**：把渲染交互、正文根、抽取 schema、翻页规则写成 JSON 放入 `RECIPES_DIR`（默认 `~/.scrape_mcp/recipes`），按主机最长后缀匹配，文件变更自动重载；单次调用的显式参数优先
- **recipes 示例**：`examples/recipes/` 提供三个真实站点 recipe（Python 文档 / GitHub Trending / 阮一峰博客），覆盖正文根、列表抽取与翻页
- **`web_crawl` 分页模式**：传 `next_selector`（或由 recipe 提供）即按"下一页"顺序翻页，`max_pages` 封顶（默认 5），已访问 URL 去重、不跟随站外链接；返回体新增 `mode` 区分 `links` / `paginate`
- **`jsonld` 字段类型**：用点号 `path` 从 `<script type="application/ld+json">` 取值，多 script 与 `@graph` 自动聚合，数组自动展开、数字段视为下标
- **`convert` 类型转换**：支持 `int` / `float` / `date`，自动去掉千分位与货币符号，日期兼容 ISO 与 `2026年10月20日` 等写法；转换失败回落 `default` 并在 `schema_notes` 说明
- **`required` 必填校验**：缺失字段汇总到 `required_missing`，整次调用返回 `error_code=missing_required`；新增同名错误码（不可重试）
- `compact_html` 支持 `root_selector` 指定正文根，未命中自动回落内容密度选根
- `web_extract` 的 `schema` 可省略，命中 recipe 时直接复用其抽取规则

### Changed
- `web_extract` 返回体新增 `required_missing`；`web_crawl` 返回体新增 `mode`
- `web_fetch` 对非 HTML 内容新增 `kind` / `content_type` 字段；PDF 响应不截断原始字节，避免破坏文件结构
- 契约 golden 测试同步锁定新增错误码与载荷键
- `dev` 依赖加入 `pypdf`，CI 覆盖 PDF 解析路径

## [0.4.0] - 2026-10-20

### Added
- **诊断载荷**：失败与 `debug=true` 时返回 `diagnostics`（分层决策轨迹 `decisions`、`attempts`、`selector_hits`、`root`、`content_chars`、L2 交互结果），只含计数与决策，不含页面正文
- **retryable 判定**：所有失败载荷新增布尔字段 `retryable`，便于调用方决定是否重试
- **Retry-After 有界等待**：429/503 带 `Retry-After` 时，可在 `RETRY_AFTER_MAX_WAIT` 上限内等待后重试，并在 `decisions` 记录 `retry:retry_after_Ns`
- **条件请求缓存**：缓存过期后带 `ETag` / `Last-Modified` 发起条件请求，命中 `304` 复用本地正文并刷新时间戳
- **结构化日志**：日志统一走 stderr 输出 JSON 行（避免污染 stdio 协议通道），通过 `SCRAPE_MCP_LOG_LEVEL` 控制级别
- `web_fetch` / `web_extract` / `web_batch` 新增 `debug` 参数
- 夜间真实站点回归工作流（每日定时跑 `scripts/demo.py`，失败自动开 issue）
- 打 tag 自动发版工作流（版本一致性校验 + 构建 + Trusted Publishing + GitHub Release）
- 契约 golden 测试：锁定各工具载荷键集合与全部错误码取值，防止无意破坏协议
- 覆盖率门禁：CI 要求 `core` / `extract` 合计覆盖率 ≥ 80%

### Changed
- `scripts/demo.py` 全部用例失败时以非零码退出，便于 CI 判定

## [0.3.0] - 2026-10-20

### Added
- **web_crawl**：从起始页按 `depth` 分层发现站内链接并逐页抓取，`limit` 控制总页数，静态资源链接自动跳过
- **结构化抽取深化**：`object` 类型与 `list` + `fields` 对象数组；`regex`/`group` 后处理、`abs_url` 相对链接转绝对、`strip` 开关
- **输出格式**：`web_fetch` / `web_batch` 新增 `format=markdown|text|json`
- **稳定错误码**：所有失败返回 `error_code`（`timeout`/`dns`/`tls`/`connection`/`blocked`/`login_required`/`http_4xx`/`http_5xx`/`empty`/`denied`/`robots_disallowed`/`invalid_argument`）
- **同域并发闸门**：同一主机并发上限 `host_max_concurrency`（默认 2），跨主机仍并行
- **/healthz**：HTTP 传输模式下的健康检查端点
- **Dockerfile**：默认仅 L1；`--build-arg INSTALL_BROWSER=1` 可打包 L2 浏览器
- `web_fetch` / `web_extract` 新增抓取参数：`headers`、`cookies`、`timeout`、`bypass_cache`
- `web_fetch` / `web_extract` 新增 L2 渲染参数：`wait_for`（等待选择器）、`scroll`（滚动懒加载）、`click`（点"加载更多"）
- `web_batch` 支持批级 `headers` / `cookies` / `timeout` / `bypass_cache`
- 传输层指数退避重试：网络异常与 5xx 自动重试（`retry_times` / `retry_backoff`），429/403 不重试
- 返回体新增 `attempts`（实际尝试次数）与 `notes`（L2 交互未命中提示）

### Changed
- 带自定义 `headers` / `cookies` 的请求不再读写共享缓存，避免把个性化响应缓存给其他调用
- 嵌套 schema 的非法选择器现在会把对应字段置空并带上完整字段路径提示，而不是整字段缺失

### Fixed
- 4xx（如 404/410）错误页不再被当作成功内容返回，统一返回 `error_code=http_4xx`

## [0.2.1] - 2026-09-18

### Added
- CLI 零配置入口：`scrape-mcp` 默认以 stdio 运行，任意目录可用；支持 `--transport`/`--port`/`--version`/`--help`

### Fixed
- 修复全新安装时 `selectolax>=1.0` 废弃 modest 后端导致 `ImportError`、命令无法启动的问题；改用官方推荐的 lexbor 后端（`LexborHTMLParser`/`LexborNode`），依赖下限改为 `selectolax>=0.4.11`

### Changed
- PyPI 发行包名调整为 `mcp-scrape`（因与已有项目 `scrapemcp` 名称相似被拒）；命令行工具名仍为 `scrape-mcp`

## [0.2.0] - 2026-09-18

在 v0.1.0 基础上新增结构化抽取与合规限流，并完善上手体验。

### Added
- **web_extract**：按字段 schema 从 HTML 抽取结构化 JSON（`text`/`attr`/`count`/`list` 类型、简写选择器、`default` 兜底、`missing` 如实上报）
- 阶段 0 合规：`respect_robots`（robots.txt 遵循）、`allowed_hosts`/`denied_hosts` 白黑名单、`max_qps` 全局限流
- `scripts/demo.py`、`scripts/smoke_extract.py`：一键演示与 HTTP 冒烟
- README 增加 60 秒入门、MCP 客户端接入教程、真实站点实测用例集

### Changed
- 抓取逻辑提取为 `_fetch_html`，供 `web_fetch`/`web_extract` 共用，避免重复抓取

### Engineering
- ruff + pre-commit 工程化配置，`pyproject.toml` 增加 dev 依赖与 lint 规则
- CI 增加 ruff 检查步骤
- `CONTRIBUTING.md`、`CODE_OF_CONDUCT.md`；README 增补发版流程

## [0.1.0] - 2026-09-16

首个可用的 MCP Server 版本，完成 M1–M5 全链路。

### Added
- **web_fetch**：反爬感知抓取，L1 curl_cffi → L2 Playwright 梯度升级
- **compact 精简**：密度选根 + DOM 剪枝，将整页 HTML 压缩为极省 token 的正文（MDN 214KB → 数百 token）
- **拦截如实上报**：`blocked`/`block_reason`，不把挑战页/空壳页当正文喂给模型
- **login / 登录态持久化**：手动登录落盘，`web_fetch` 自动带上、过期提示重登
- **web_batch**：并发抓取一批 URL，单条异常不影响整批
- **磁盘缓存**：L1 明文响应按 TTL 缓存
- **Stealth 加固**：Wipe webdriver、Canvas/WebGL 指纹噪声、时区对齐等
- **传输模式**：stdio / streamable-http（Postman/curl 直连）双支持
- **配置**：全部可通过 `SCRAPE_MCP_*` 环境变量或项目根 `.env` 覆盖
- 44 项单元测试 + GitHub Actions CI（Python 3.10–3.13）