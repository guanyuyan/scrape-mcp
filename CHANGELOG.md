# Changelog

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。所有显著变更都会记录在本文件。

## [Unreleased]

### Added
- ruff + pre-commit 工程化配置，`pyproject.toml` 增加 dev 依赖与 lint 规则
- CI 增加 ruff 检查步骤
- `CONTRIBUTING.md`、`CODE_OF_CONDUCT.md`

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