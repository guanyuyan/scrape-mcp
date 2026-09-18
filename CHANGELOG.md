# Changelog

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。所有显著变更都会记录在本文件。

## [Unreleased]

(暂无)

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