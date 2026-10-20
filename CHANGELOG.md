# Changelog

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。所有显著变更都会记录在本文件。

## [Unreleased]

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