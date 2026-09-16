# scrape-mcp

> **⚠️ 这是 scrape-mcp 的官方源仓库（upstream）。** 若在别处看到同名/相似项目并带不同作者署名，即为转载或派生，非原作者发布。

> **反爬感知抓取 + 极省 Token 内容精简的 MCP Server —— 让 AI 读真实网页时，只花 1%~5% 的 token 就能拿到干净、真实、免登录的正文。**
>
> 自动化分三层应对：默认 curl_cffi（伪装 TLS/JA3 + HTTP2）低成本打头阵，命中 Cloudflare/验证码/WAF 才升级 Playwright 真浏览器渲染；遇到"登录才给内容"的站，先弹窗手动登录一次、登录态落盘，之后自动带上、过期会提示重登。

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/guanyuyan/scrape-mcp?color=blue&label=release)](https://github.com/guanyuyan/scrape-mcp/releases)
[![CI](https://github.com/guanyuyan/scrape-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/guanyuyan/scrape-mcp/actions)

## 功能说明

**它解决什么**：模型读网页不是在读正文，而是在读整棵 DOM（脚本、导航、版权、链接一应尽收），又贵又脏。这个工具把页面剪成"只有正文"再喂给 AI。

**核心价值（三点）**
1. **极省 token** —— 属性剥离 + 模板块丢弃 + 站外链接降级 + 表格转 CSV + 预算截断。实测 MDN 整页 214KB → 数百 token，压缩率约 1%。
2. **自动分级反爬** —— 85% 的站只走最轻的 L1（快、并发高），拦截/JS 骨架页才升级 L2 真浏览器 + stealth 伪装，绝不暴力逐站上浏览器。
3. **登录墙处理 + 如实上报** —— 判定登录/拦截时返回 `login_required`/`blocked`，引导登录而非硬撞，从不让模型把验证码当正文。

**怎么用（一行）**
```
web_fetch(url="https://xxx/article/1") → JSON：{ok, content(极省正文), token_estimate, tier, blocked?, login_required?, cached?}
```

**覆盖的站点类型**：文章页 / 文档站 / 门户与列表页 / 需要 JS 渲染的 SPA / 需要登录才给正文的站（如知乎）。

**形态**：本地 MCP Server（stdio/HTTP 均可），隐私可控；支持单页 `web_fetch`、批量 `web_batch`、登录持久化 `login`，缓存默认开启。

---

## 特性

- **分级抓取**：L1 `curl_cffi` → L2 Playwright，拦截才升级，不误伤正常页面。
- **内容密度选根**：从 article/main/div 中挑出"恰好装住正文的最紧容器"，避开导航/侧栏。
- **compact 精简**：属性剥离、模板块丢弃、站外链接降级、表格转 CSV、token 预算截断，
  压缩率可达 1~20% 数量级（如 MDN 214KB → 数百 token）。
- **拦截如实上报**：不把挑战页/空壳页当正文喂给模型，返回 `blocked`/`block_reason`。
- **登录态持久化**：`login` 工具手动登录并落盘，`web_fetch` 自动带上；过期会提示重登。
- **缓存与批量**：L1 明文响应按 TTL 磁盘缓存；`web_batch` 并发抓取一批 URL。
- **Stealth 加固**：Wipe `webdriver`、UA 数据、Canvas/WebGL 确定性指纹噪声、时区对齐。

## 安装

```bash
# L2 渲染所需浏览器（国内可用镜像）
$env:PLAYWRIGHT_DOWNLOAD_HOST="https://cdn.npmmirror.com/binaries/playwright"
pip install "playwright>=1.49"
playwright install chromium

# 或复用系统浏览器，无需下载
$env:SCRAPE_MCP_L2_CHANNEL="chrome"   # 或 msedge
```

## 启动（MCP，stdio）

```bash
scrape-mcp
# SSL_DIR 切换协议：stdio（默认）
```

## 环境变量（前缀 `SCRAPE_MCP_`）

全部配置可用环境变量（`SCRAPE_MCP_` 前缀）设置，也可集中放到**项目根目录 `.env`** 文件里。

```bash
# 项目根目录执行
cp .env.example .env    # 生成配置模板
# 编辑 .env 后无需改代码，直接启动即可
```

优先级：实际进程环境变量 > `.env` > 默认值。`.env` 被 gitignore，不入库；`.env.example` 是随仓库提交的模板。

| 变量 | 默认 | 说明 |
|---|---|---|
| `IMPERSONATE` | `chrome` | curl_cffi 指纹模板 |
| `REQUEST_TIMEOUT` | `20` | L1 超时（秒） |
| `PROXY` | — | 代理，L1/L2 共用 |
| `L2_ENABLED` | `true` | 是否启用 Playwright 升级链 |
| `L2_CHANNEL` | — | 关联浏览器：空=自带 Chromium，`chrome`/`msedge`=系统 |
| `L2_SETTLE_TIMEOUT` | `8` | 挑战页等待窗口（秒） |
| `L2_TIMEZONE` | `Asia/Shanghai` | 隐身加固时区 |
| `DATA_DIR` | `~/.scrape_mcp` | 登录态/缓存放盘目录 |
| `CACHE_TTL` | `300` | L1 缓存 TTL（秒），`0` 关闭 |
| `BATCH_MAX_CONCURRENCY` | `4` | `web_batch` 并发上限 |
| `BATCH_MAX_URLS` | `20` | `web_batch` 单批上限 |
| `RESPECT_ROBOTS` | `true` | 默认遵循目标站 robots.txt；`false` 忽略（仅建议自用合规场景关闭） |
| `ALLOWED_HOSTS` | — | 主机白名单（逗号分隔）；非空时仅允许名单内主机 |
| `DENIED_HOSTS` | — | 主机黑名单（逗号分隔），优先于白名单 |
| `MAX_QPS` | `10` | 全网抓取限流（每秒请求数）；`0` 不限速 |

## HTTP 模式（Postman / curl 直连）

默认 stdio，改环境变量即可切到 HTTP，无需改代码：

```bash
$env:SCRAPE_MCP_TRANSPORT="streamable-http"
$env:SCRAPE_MCP_HTTP_PORT="8000"
python -m scrape_mcp.server
```

以 `json_response=1`（默认）启动，请求/响应都是普通 JSON（非事件流）。**stateless 模式无需先 `initialize`，直接发 `tools/call` 即可。**

必需项：请求头 `MCP-Method`、`Mcp-Name`；body 内 `params` 需带 `_meta`（`protocolVersion` + `clientCapabilities`）。完整示例见下。

## MCP 工具

- **`web_fetch(url, max_tokens=4000, link_policy="internal")`** → 状态 + 极省正文
  - `link_policy`: `internal`（站内相对链接+站外降级）/ `all` / `none`
- **`web_batch(urls, max_tokens=3000, link_policy="internal")`** → 并发抓取一批，逐 URL 返回；单条异常不影响整批
- **`login(url, timeout=180)`** → 打开带界面浏览器手动登录并持久化登录态

返回为 JSON 字符串（落在 `content[0].text`，请解析文本，勿依赖 `structured_content`）。

## 目录结构

```
src/scrape_mcp
├── server.py          # MCP 入口 + 工具（web_fetch/web_batch/login）
├── config.py          # 环境变量配置
├── tokenizer.py       # tiktoken 计数 + 预算截断
├── core/
│   ├── http_client.py # L1 curl_cffi + 磁盘缓存
│   ├── fetcher.py     # 分级调度（L1→L2），对工具层返回 FetchOutcome
│   ├── detector.py    # 拦截/骨架页判定
│   ├── browser.py     # L2 Playwright + stealth
│   ├── session.py     # 登录态持久化
│   └── cache.py       # HTTP 响应缓存
└── extract/
    └── compact.py     # DOM 剪枝 → 极省 token 正文
```

## 开发

```bash
pip install -e ".[dev]"
python -m pytest
```