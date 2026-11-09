# scrape-mcp

> **⚠️ 这是 scrape-mcp 的官方源仓库（upstream）。** 若在别处看到同名/相似项目并带不同作者署名，即为转载或派生，非原作者发布。

> **反爬感知抓取 + 极省 Token 内容精简的 MCP Server —— 让 AI 读真实网页时，只花 1%~5% 的 token 就能拿到干净、真实、免登录的正文。**
>
> 自动化分三层应对：默认 curl_cffi（伪装 TLS/JA3 + HTTP2）低成本打头阵，命中 Cloudflare/验证码/WAF 才升级 Playwright 真浏览器渲染；遇到"登录才给内容"的站，先弹窗手动登录一次、登录态落盘，之后自动带上、过期会提示重登。

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/mcp-scrape?color=blue&label=pypi)](https://pypi.org/project/mcp-scrape/)
[![Python](https://img.shields.io/pypi/pyversions/mcp-scrape)](https://pypi.org/project/mcp-scrape/)
[![Release](https://img.shields.io/github/v/release/guanyuyan/scrape-mcp?color=blue&label=release)](https://github.com/guanyuyan/scrape-mcp/releases)
[![CI](https://github.com/guanyuyan/scrape-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/guanyuyan/scrape-mcp/actions)

简体中文 | [English](README.en.md)

---

## ⚡ 60 秒入门

**方式 A：一键安装（推荐，零配置）**

```bash
pip install mcp-scrape
# 或从源码：cd scrape-mcp && pip install -e .
```

然后本机任意目录即可：

```bash
scrape-mcp --version          # 确认装好（应输出 0.x）
claude                        # 在支持 MCP 的客户端里把它当服务接入
```

> 不再需要 clone、手动设 `PYTHONPATH` 或 `python -m`；`scrape-mcp` 命令在任意目录可用，
> 默认 stdio 直接被 MCP 客户端接管。想用 HTTP 直连（Postman/curl），跑 `scrape-mcp --transport streamable-http --port 8000`。

**方式 B：一键演示**（拉源仓库跑 `web_fetch` + `web_extract`，看省 token 效果）

```bash
git clone https://github.com/guanyuyan/scrape-mcp.git
cd scrape-mcp
python -m venv .venv && .venv\Scripts\pip install -e .
.venv\Scripts\python scripts\demo.py
```

预计输出（对不同站点自动选 L1/L2 分级，给出省 token 压缩比）：

```
=== 文档站 | https://developer.mozilla.org/zh-CN/docs/Web/HTTP/CORS
  ok=True tier=l1 status=200 html=260128B → tokens=3730 (约 5.7% 压缩)
  标题: 跨源资源共享（CORS） - HTTP | MDN
  ok=True tier=l1 data={"heading": "跨源资源共享（CORS）", "h2_count": 10, "links": 665}
```

**第 2 步：接入 MCP 客户端**（Claude / Cursor 等，一条命令写好配置）

```bash
scrape-mcp init --client cursor --write     # claude / vscode / json 同理
```

默认只打印 diff 预览，确认后加 `--write` 落盘；重启客户端即可调用 `web_fetch(url=...)`。

> MDN 整页约 260KB，这里只花几千 token 就拿到干净正文；拦截/登录页会自动升级或如实上报，不会把验证码当正文。

---

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

**真实站点实测**（一键 `scripts/demo.py` 可复现）：

| 站点 | 类型 | 分级 | 原始 | 抓到正文 | 说明 |
|---|---|---|---|---|---|
| MDN CORS 文档 | 文档站 | L1 | 260KB | ~3.7k token（约 5.7%） | 纯静态，最短路径直达 |
| 腾讯新闻首页 | 列表/门户 | L2 | 338KB | ~1.4k token（约 1.6%） | 动态页升级浏览器渲染 |
| 极客邦首页 | SPA/动态 | L2 | 12KB | ~1.4k token | JS 骨架页，需渲染才有正文 |

**形态**：本地 MCP Server（stdio/HTTP 均可），隐私可控；支持单页 `web_fetch`、批量 `web_batch`、结构化抽取 `web_extract`、变更检测 `web_watch`、定时监控 `web_schedule`、搜索 `web_search`、截图 `web_screenshot`、站点 URL 发现 `web_map`、sitemap 增量索引 `web_index`、异步爬取任务 `web_crawl_start/status/cancel/list`、登录持久化 `login`，缓存默认开启。

---

## 特性

- **分级抓取**：L1 `curl_cffi` → L2 Playwright，拦截才升级，不误伤正常页面。L2 真拿到正文后按主机记 10 分钟"必须上浏览器"，期间同站直接走 L2（`skip_l1:sticky_l2`），省掉每次注定被拦的 L1；TTL 到期或浏览器渲染失败会自动回到 L1 重新判断
- **L1 连接复用**：同一指纹模板在进程内共享连接池，省掉重复的 TLS 握手与 DNS 解析；换事件循环自动重建，进程退出或 `aclose()` 统一释放。
- **L2 浏览器池**：同站同会话复用 context（各带独立登录态），超出上限按最久未用淘汰；同时打开的页面数受限，超出排队；空闲超过 `L2_IDLE_TIMEOUT` 秒自动关掉浏览器进程，下次用到再启动。
- **内容密度选根**：从 article/main/div 中挑出"恰好装住正文的最紧容器"，避开导航/侧栏。
- **compact 精简**：属性剥离、模板块丢弃、站外链接降级、表格转 CSV（合并单元格自动补齐空位）、
  代码块带语言标记、`figure` 输出图片地址与图注，压缩率可达 1~20% 数量级（如 MDN 214KB → 数百 token）。
- **拦截如实上报**：不把挑战页/空壳页当正文喂给模型，返回 `blocked`/`block_reason`。
- **稳定错误码 + 可诊断**：失败返回 `error_code` 与 `retryable`，`debug=true` 还能看到分层决策、选择器命中数与 L2 交互结果。
- **登录态持久化 + 多会话**：`login` 工具手动登录并落盘，`web_fetch` 自动带上；`session="work"` 可按身份隔离登录态，`session_export`/`session_import` 在机器之间搬运 cookie。无 GUI 服务器可 `headless=true` 登录（截图落盘供查看/扫码），或用 `qr_selector` 只截二维码，也可用 `cdp_url` 连远程浏览器。
- **非 HTML 内容**：`web_fetch` 直接读纯文本；DOCX/XLSX/PPTX 走标准库抽取文字层（无需额外依赖）；装 `[pdf]` extra 后按页抽取 PDF 文本；扫描件（无文字层）如实返回 `empty`，开启 `[ocr]` extra 后可选 OCR，不伪造正文。
- **同 URL 单飞（singleflight）**：同一页面被并发请求时（多租户同时抓、批量/爬取里重复链接）只真抓一次，其余等待并复用同一份响应，省出口流量也少踩目标站风控；L1 与 L2 渲染都会合并。带自定义头/凭证或 `bypass_cache=true` 的请求可能因人而异，不参与合并。
- **两层缓存**：HTTP 响应按 TTL 落盘，过期后带 `ETag`/`Last-Modified` 做条件请求；正文（compact 后）与字段抽取结果另有一层结果缓存，二次调用直接返回，`bypass_cache=true` 同时绕过两层。`web_batch` 并发抓取一批 URL。
- **轻量爬取**：`web_crawl` 支持两种模式——按 `depth`/`limit` 发现站内链接，或按 `next_selector` 顺序翻页（`max_pages` 封顶、自动去重、不跳站外）；静态资源自动跳过。页数多时改用 `web_crawl_start` 起后台任务，用 `status` 轮询、`cancel` 停止，进程重启后自动续跑。
- **变更检测**：`web_watch` 每次成功抓取后留一份正文快照，下次与它对比并返回 `changed` 与增删行摘要；指定 `selector` 可只盯页面某一块（如榜单、价格区）。抓取失败不覆盖旧快照，风控恢复后仍能对比出这期间的真实变化。
- **定时监控与通知**：`web_schedule` 把 `web_watch` / `web_index` 变成计划任务（任务可查、可暂停、可手动跑一次），由本机 `scrape-mcp schedule daemon` 按间隔执行；检测到真实变更后 POST 到你配置的 webhook。首次执行只建立基线，不发通知；抓取失败只记状态，不覆盖快照。
- **搜索发现**：`web_search` 把关键词变成结构化结果（标题 / URL / 摘要 / 来源引擎 / 发布时间），默认走 DuckDuckGo HTML 端点（无需 API key），也可指向自托管 SearXNG 的 JSON API 或配置 key 使用 Brave / Tavily；支持 `site`、`time_range`、`engines` 过滤与后端失败自动回退，结果可缓存，适合"先搜再抓"的两步流程。
- **页面截图**：`web_screenshot` 用真实浏览器渲染后导出 PNG 或整页 PDF，支持整页/视口、自定义尺寸、元素裁剪、设备预设（desktop/mobile）、截图前隐藏固定导航/弹窗，以及等待元素与额外延时；`actions` 可在截图前先点击/输入/翻页。复用 `login` 保存的登录态，需要登录或 JS 渲染的页面也能拍。
- **站点索引**：`web_index` 读 robots.txt 的 `Sitemap:`（回退 `/sitemap.xml`），递归展开 sitemap index 并解压 `.gz`；清单落盘后，下次返回新增 / 变更 / 未变 / 移除，可选回抓正文比哈希过滤"只改了时间戳"的假变更，或输出与 `web_watch` 同口径的差异摘要。
- **站点 URL 发现**：`web_map` 同时读 sitemap 与入口页站内链接（可选合并 `web_search` 结果），同一 URL 只出现一次并标出来源；支持 `include`/`exclude` 正则、`search` 子串过滤与 `limit` 截断，适合无 sitemap 的站点。
- **异步爬取任务**：`web_crawl_start` 立即返回 `job_id`，任务在后台推进并把状态逐页原子落盘到 `DATA_DIR/jobs/`；`web_crawl_status` 可读进度与部分结果、并在进程重启后自动续跑，`web_crawl_cancel` 停止任务，`web_crawl_list` 列出最近任务。同参数重复 start 幂等。
- **内置运行看板**：HTTP 模式下打开 `/` 即见请求量、L1/L2 分布、耗时分位与分桶、结果码、正文类型、L2 页位与代理池健康度；单文件 HTML，无外部依赖，每 5 秒自动刷新。
- **总预算与降级**：`web_fetch` / `web_extract` / `web_batch` / `web_crawl` / `web_map` / `web_crawl_start` 都支持 `deadline_ms`。预算不够时按"压缩单步超时 → 跳过重试 → 不升级 L2 → 直接返回 `timeout`"逐级降级，已拿到的结果照常返回；降级结果不写入缓存，避免后续预算充足的调用拿到残缺内容。预算同样覆盖合规检查（限流等待与 robots.txt 抓取），不够就提前返回 `timeout`，不把时间耗在准备阶段。
- **结构化抽取**：`web_extract` 支持 `text`/`html`/`attr`/`count`/`list`/`object`/`jsonld`，配 `regex`、`abs_url`、`convert`（int/float/date）与 `required` 必填校验，缺字段直接返回 `missing_required`；第三方包可通过入口点注册自定义字段类型。
- **站点 recipe**：把某站的渲染方式、正文选择器、抽取 schema、翻页规则写成一份 JSON 落到本地，之后同一站点免参数复用，单次调用仍可覆盖。
- **输出格式可选**：`format=markdown|text|json|chunks`；`json` 给段落数组，`chunks` 直接给 RAG 分块（固定窗口 + overlap + 标题路径），可原样入库。
- **同域并发闸门**：同一站点默认最多 2 个并发请求，跨站仍可并行，避免批量抓取时把单站打穿。
- **Stealth 加固**：Wipe `webdriver`、UA 数据、Canvas/WebGL 确定性指纹噪声、时区对齐。
- **挑战诊断**：被拦截时载荷带 `challenge`（厂商 / 类型 / `sitekey` / `action` / 状态码 / `Retry-After`），把"为什么抓不到"说清楚，便于决定换出口还是交人工。
- **求解闭环**：可选求解钩子（默认关闭）+ cookie 缓存 + L1/L2 重试，同一站点同一会话只解一次；不内置破解。
- **住宅出口**：代理池支持 `residential` 类别，被限流/挑战且预算允许时只升级一次住宅出口，凭据全程脱敏。

## 反爬与出口

### 挑战页诊断

页面被判为拦截（`blocked` 非 `none`）时，抓取结果会多一个 `challenge` 字段，正常页面为 `null`：

```json
{
  "ok": false,
  "error_code": "blocked",
  "blocked": "waf",
  "challenge": {
    "provider": "recaptcha",
    "kind": "captcha",
    "sitekey": "6Lcabc",
    "action": "login",
    "status": 403,
    "retry_after": null,
    "evidence": "命中 recaptcha 特征: g-recaptcha"
  },
  "diagnostics": {
    "decisions": ["challenge:recaptcha:captcha", "challenge:sitekey=6Lcabc"],
    "attempts": 1
  }
}
```

- `provider`：`cloudflare` / `turnstile` / `datadome` / `akamai` / `perimeterx` / `recaptcha` / `hcaptcha` / `unknown`
- `kind`：`js_challenge` / `interstitial` / `captcha` / `rate_limit` / `login_wall`
- `sitekey` 只回传前 6 位（求解服务收到的是完整值）；普通 403/404 错误页不带挑战特征，保持 `null`
- 只扫描正文前 200 KB，避免长文里的"验证码"教程造成误判

### 求解与住宅升级

- 求解钩子默认关闭：`SCRAPE_MCP_CAPTCHA_SOLVER_URL` 留空时整条链路 no-op。配置后把 `{url,status,blocked,provider,kind,sitekey,action}`（不含正文）发给你的合规服务，接受 `{ok,cookies,note,user_agent}` 并重试：先 L1，仍被拦且预算允许再渲染一次 L2
- 求解缓存：`SCRAPE_MCP_CAPTCHA_COOKIE_CACHE=true` 时按 `(session, host)` 复用求解 cookie（TTL 默认 900 秒，LRU 256，落盘权限 600）；同一站点第二次被拦先用缓存重试，命中不再调用求解服务。cookie 属敏感数据，多租户共用进程请勿开启
- 住宅升级：配置 `SCRAPE_MCP_PROXY_PROVIDER_TEMPLATE`（`{user}`/`{pass}`/`{host}`/`{region}`/`{session}`）或在代理池写 `url|region|residential`；完整链路仍命中 `rate_limit` / `cloudflare` / `captcha` / `waf` 且预算足够时只升级一次（`decisions` 含 `egress:escalate_residential`），显式 `proxy` 与会话绑定代理不改写
- 凭据脱敏：`notes`、日志与代理池快照都不含账号密码，必要时显示为 `***`

## 安装

```bash
pip install mcp-scrape                          # 核心：L1 抓取 + 精简 + 抽取
pip install "mcp-scrape[pdf]"                   # 可选：让 web_fetch 直接读 PDF
pip install "mcp-scrape[ocr]"                   # 可选：扫描 PDF 的 OCR（另需系统 tesseract 与语言包）
pip install "mcp-scrape[langchain]"             # 可选：LangChain 工具适配
pip install "mcp-scrape[llamaindex]"            # 可选：LlamaIndex Reader 适配
```

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
scrape-mcp                          # 默认 stdio，被 MCP 客户端接管
scrape-mcp --transport streamable-http --port 8000   # 切 HTTP 直连
scrape-mcp --version / --help       # 版本 / 用法
```

> 不传参默认 stdio。传 `--transport`/`--port` 由 CLI 显式启动，优先级高于 `.env`/环境变量。

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
| `PROXY` | — | 单个代理，L1/L2 共用；配置了代理池时作为兜底 |
| `PROXY_POOL` | — | 代理池（逗号/分号/空白分隔），按站点粘性轮询；非空时优先于 `PROXY`。条目可写 `url\|region\|kind`，`kind` 取 `dc`（默认）/ `residential`，住宅条目只用于被拦后的一次升级 |
| `PROXY_POOL_FAIL_THRESHOLD` | `2` | 同一出口连续失败多少次后进入冷却 |
| `PROXY_POOL_COOLDOWN` | `60` | 出口冷却时长（秒），冷却期内不再分配 |
| `PROXY_POOL_STICKY_TTL` | `300` | 同一主机固定走同一出口的时长（秒） |
| `PROXY_SESSIONS` | — | 会话绑定代理（`名字=代理` 条目，逗号/分号/空白分隔），如 `work=http://1.2.3.4:8080`；同一会话（含登录态）固定走该出口。优先级：单请求 `proxy` > 会话绑定 > 代理池 > `PROXY` |
| `CAPTCHA_SOLVER_URL` | — | 可选 CAPTCHA 服务钩子，留空即关闭（默认）。配置后仅在检测到人机验证时把 URL/状态码/拦截类型（识别到挑战时附带 `provider`/`kind`/`sitekey`/`action`）转给该端点，接受其返回的 `cookies` 做一次重试 |
| `CAPTCHA_SOLVER_KEY` | — | 钩子服务的 Bearer 令牌（可选） |
| `CAPTCHA_SOLVER_TIMEOUT` | `10` | 钩子调用超时（秒） |
| `CAPTCHA_COOKIE_CACHE` | `false` | 是否把求解得到的 cookie 按 `(session, host)` 缓存复用；cookie 属敏感数据，多租户共用进程时请勿开启 |
| `CAPTCHA_COOKIE_TTL` | `900` | 求解 cookie 缓存有效期（秒）；过期视为未命中 |
| `PROXY_PROVIDER_TEMPLATE` | — | 住宅代理服务商模板，留空不启用住宅升级；支持 `{user}` / `{pass}` / `{host}` / `{region}` / `{session}` 占位符，启动时用假值预展开校验 |
| `PROXY_PROVIDER_USER` / `PROXY_PROVIDER_PASS` | — | 模板中的账号密码（只出现在展开后的代理 URL，日志与 `notes` 脱敏） |
| `PROXY_PROVIDER_STICKY` | `true` | 按站点派生稳定 session（`true`，同站点固定同一住宅 IP）或每请求轮换（`false`） |
| `L2_ENABLED` | `true` | 是否启用 Playwright 升级链 |
| `L2_CHANNEL` | — | 关联浏览器：空=自带 Chromium，`chrome`/`msedge`=系统 |
| `L2_SETTLE_TIMEOUT` | `8` | 挑战页等待窗口（秒） |
| `L2_SCROLL_STEPS` | `5` | L2 滚动步数（触发懒加载） |
| `L2_SCROLL_DELAY_MS` | `400` | L2 每步滚动间隔（毫秒） |
| `L2_MAX_PAGES` | `3` | L2 同时打开的页面上限，超出排队 |
| `L2_MAX_CONTEXTS` | `8` | L2 常驻 context 上限（按 host + session 复用），超出淘汰最久未用 |
| `L2_IDLE_TIMEOUT` | `120` | L2 浏览器空闲回收阈值（秒），`0` 常驻到进程退出 |
| `REMOTE_BROWSER_CDP_URL` | 空 | 远程浏览器池的 CDP 端点（http/https/ws/wss）；留空本地启动 |
| `REMOTE_BROWSER_FALLBACK` | `true` | 远程连接失败时是否回退本地浏览器渠道；`false` 直接报错 |
| `L2_TIMEZONE` | `Asia/Shanghai` | 隐身加固时区 |
| `DATA_DIR` | `~/.scrape_mcp` | 登录态/缓存放盘目录 |
| `RECIPES_DIR` | `<DATA_DIR>/recipes` | 站点 recipe 目录；留空用默认路径 |
| `PDF_MAX_PAGES` | `200` | PDF 单次最多解析页数（超出部分不抽取，`pages` 仍返回总页数） |
| `OCR_ENABLED` | `false` | 是否对无文字层的 PDF 启动 OCR（需 `[ocr]` extra + 系统 tesseract） |
| `OCR_LANG` | `chi_sim+eng` | tesseract 语言包组合 |
| `OCR_DPI` | `200` | 渲染页面时的 DPI（越高越准，也越慢） |
| `OCR_MAX_PAGES` | `10` | 单次 OCR 最多识别页数 |
| `CACHE_TTL` | `300` | 两层缓存 TTL（秒），`0` 全关；HTTP 层过期后带 `ETag`/`Last-Modified` 做条件请求，正文/抽取结果层命中即直接返回 |
| `RETRY_TIMES` | `2` | 网络异常/5xx 重试次数（429/403 不重试） |
| `RETRY_BACKOFF` | `0.5` | 重试退避基数（秒，指数增长） |
| `RETRY_AFTER_MAX_WAIT` | `0` | 429/503 带 `Retry-After` 时最多等待秒数；`0` 只上报不等待 |
| `LOG_LEVEL` | `WARNING` | 日志级别，JSON 行写 stderr（stdout 留给 MCP 协议） |
| `BATCH_MAX_CONCURRENCY` | `4` | `web_batch` 并发上限 |
| `BATCH_MAX_URLS` | `20` | `web_batch` 单批上限 |
| `HOST_MAX_CONCURRENCY` | `2` | 同一主机的并发上限（`web_batch`/`web_crawl` 共用） |
| `CRAWL_MAX_PAGES` | `20` | `web_crawl` 单次最多抓取页数硬上限 |
| `SEARCH_PROVIDER` | `duckduckgo` | `web_search` 后端：`duckduckgo`（HTML 端点，无需 key）/ `searxng`（自托管 JSON API）/ `brave` / `tavily`（商业 API，需 key） |
| `SEARCH_SEARXNG_URL` | — | SearXNG 实例根地址，如 `http://127.0.0.1:8080`；`provider=searxng` 时必填 |
| `SEARCH_BRAVE_API_KEY` | — | Brave Search API key；留空则该后端不可用，也不参与回退 |
| `SEARCH_TAVILY_API_KEY` | — | Tavily API key；留空则该后端不可用，也不参与回退 |
| `SEARCH_FALLBACK` | `true` | 后端失败时是否自动回退到其他已配置的后端（duckduckgo → searxng → brave → tavily） |
| `SEARCH_MAX_RESULTS` | `20` | `web_search` 单次返回条数硬上限 |
| `LLM_BASE_URL` | — | OpenAI 兼容端点根地址（如 `https://api.deepseek.com/v1`）；留空则完全不发起 LLM 调用 |
| `LLM_API_KEY` | — | LLM 端点 key（Bearer）；本地兼容端点可不填 |
| `LLM_MODEL` | — | 模型名；与 `LLM_BASE_URL` 同时配置才启用 LLM 辅助抽取 |
| `LLM_TIMEOUT` | `60` | 单次 LLM 请求超时（秒） |
| `LLM_MAX_INPUT_TOKENS` | `6000` | 送入模型的正文 token 预算（compact 后截断） |
| `LLM_MAX_OUTPUT_TOKENS` | `2048` | 模型输出上限 |
| `DATASET` | — | 数据集导出路径（JSONL，一行一次成功抓取）；留空不导出 |
| `RESPECT_ROBOTS` | `true` | 默认遵循目标站 robots.txt；`false` 忽略（仅建议自用合规场景关闭） |
| `ALLOWED_HOSTS` | — | 主机白名单（逗号分隔）；非空时仅允许名单内主机 |
| `DENIED_HOSTS` | — | 主机黑名单（逗号分隔），优先于白名单 |
| `MAX_QPS` | `10` | 全网抓取限流（每秒请求数）；`0` 不限速 |
| `HTTP_TOKEN` | — | HTTP 模式访问令牌；设置后除 `/healthz` 与看板空壳 `/` 外都需 Bearer，绑定非回环地址时必填（与多令牌互斥） |
| `HTTP_TOKENS` | — | 多令牌 JSON：`[{"name","token","qps","daily_quota","tools"}]`，每个租户独立配额与可选工具白名单；与 `HTTP_TOKEN` 互斥 |
| `HTTP_TOKENS_FILE` | — | 同格式 JSON 文件路径；适合把令牌放卷上保管（与 `HTTP_TOKENS` 互斥） |
| `HTTP_QPS` | `5` | 每秒请求上限（令牌桶，允许同量突发）；多令牌时作为省略 `qps` 的租户默认值；`0` 不限 |
| `HTTP_DAILY_QUOTA` | `10000` | 每日请求配额（UTC 自然日，跨日清零）；多令牌时作为省略 `daily_quota` 的租户默认值；`0` 不限 |
| `CLOUD_BROKER_ENABLED` | `false` | 云端控制面 broker 开关；开启后暴露 `/cloud/*` 租约端点 |
| `CLOUD_BROWSER_UPSTREAMS` | — | 浏览器上游（逗号/分号/空白分隔的 CDP 端点，`http/https/ws/wss`），按租户轮询分配 |
| `CLOUD_PROXY_UPSTREAMS` | — | 代理上游（`http/https/socks4/socks5`），条目可带地区标签，如 `socks5://host:port\|us` |
| `CLOUD_LEASE_TTL` | `300` | 租约有效期（秒），到期自动回收 |
| `CLOUD_MAX_LEASES_PER_TENANT` | `2` | 单租户同时持有的租约上限（浏览器/代理分别计算）；`0` 不限 |
| `CLOUD_DAILY_LEASES` | `0` | 单租户每日租约配额（UTC 自然日，浏览器/代理分别计算，与 HTTP 日配额独立）；`0` 不限 |
| `CLOUD_BROKER_REDIS_URL` | — | 租约状态改存 Redis，多实例共享租约、轮询游标与配额；留空为进程内状态。键前缀复用 `REDIS_PREFIX`，需 `mcp-scrape[redis]` |
| `JOB_BACKEND` | `local` | 异步 crawl 任务后端：`local`（本地 JSON）或 `redis`（共享状态 + 队列，需 `mcp-scrape[redis]`） |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | `JOB_BACKEND=redis` 时的连接地址 |
| `REDIS_PREFIX` | `scrape_mcp` | Redis 键前缀，便于多套环境共用实例 |
| `JOB_TTL` | `604800` | 任务状态在 Redis 中的保留秒数；`0` 不设过期 |
| `JOB_STALE_AFTER` | `600` | worker 重启后，running 超过该秒数未更新才视为失联并重新入队 |
| `SNAPSHOT_BACKEND` | `local` | `web_watch` / `web_index(content_diff)` 快照后端：`local` 或 `s3`（需 `mcp-scrape[s3]`） |
| `S3_BUCKET` | 空 | `SNAPSHOT_BACKEND=s3` 时的桶名，必填 |
| `S3_ENDPOINT_URL` | 空 | S3 兼容端点（MinIO / R2 / OSS）；留空用 AWS 默认 |
| `S3_REGION` | 空 | 区域名；自建端点通常可留空 |
| `S3_PREFIX` | `scrape_mcp` | 对象键前缀，便于多套环境共用同一个桶 |
| `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` | 空 | 留空时走 boto3 默认凭据链（环境变量 / IAM 角色） |
| `S3_PATH_STYLE` | `false` | MinIO 等通常需要设为 `true`（path-style 寻址） |
| `DATASET_BACKEND` | `local` | 数据集后端：`local` 用 `DATASET` 路径，`s3` 每条记录一个对象且无需路径 |
| `S3_JOB_ARTIFACTS` | `false` | 异步 crawl 进入终态后把任务 JSON 归档到 `<S3_PREFIX>/jobs/<id>.json` |

代理池非空时优先于 `PROXY`：出口按轮询分配，同一主机在 `PROXY_POOL_STICKY_TTL` 内固定走同一个 IP（登录态与风控都更稳）；同一出口连续失败（传输异常，或整条链路走完仍被限流/风控拦截）达 `PROXY_POOL_FAIL_THRESHOLD` 次后进入 `PROXY_POOL_COOLDOWN` 秒冷却，冷却期内不再分配；整池都在冷却时回退到最早解冻的那个，而不是直接报错。单请求 `proxy` 参数优先级最高，且不参与池的失败计数。

## HTTP 模式（Postman / curl 直连）

默认 stdio，改环境变量即可切到 HTTP，无需改代码：

```bash
$env:SCRAPE_MCP_TRANSPORT="streamable-http"
$env:SCRAPE_MCP_HTTP_PORT="8000"
$env:SCRAPE_MCP_HTTP_TOKEN="<足够长的随机字符串>"   # 绑定非回环地址时必填
python -m scrape_mcp.server
```

以 `json_response=1`（默认）启动，请求/响应都是普通 JSON（非事件流）。**stateless 模式无需先 `initialize`，直接发 `tools/call` 即可。**

**访问令牌**：设置 `SCRAPE_MCP_HTTP_TOKEN` 后，除 `GET /healthz` 与看板空壳 `GET /` 外所有请求都要带：

```bash
curl -H "Authorization: Bearer $env:SCRAPE_MCP_HTTP_TOKEN" http://127.0.0.1:8000/stats
```

未设置令牌时服务只允许绑定 `127.0.0.1` / `localhost` / `::1`；绑定 `0.0.0.0` 等非回环地址会直接退出，避免无鉴权暴露。缺失或错误令牌统一返回 401（带 `WWW-Authenticate`），响应里不含内部路径与版本。令牌只从请求头读取，不写入日志。配置令牌后回环请求同样要携带，避免本机反向代理把外部请求伪装成 `127.0.0.1` 绕过鉴权。

**配额**：配置令牌后，该令牌的请求还受 `HTTP_QPS`（默认 5 次/秒）与 `HTTP_DAILY_QUOTA`（默认每日 10000 次，UTC 零点清零）约束，超限返回 429，响应带 `Retry-After` 头与 `retry_after_s` 字段（`error` 分别为 `rate_limited` / `quota_exceeded`），客户端可按建议时间退避重试。`/healthz` 与 `/metrics` 不消耗配额；未配置令牌的纯本机模式不受配额限制。

**多令牌（多租户）**：多个调用方各拿一个令牌时，用 `SCRAPE_MCP_HTTP_TOKENS`（JSON）或 `SCRAPE_MCP_HTTP_TOKENS_FILE`（同格式文件，适合挂卷保管）一次配置多组令牌，每组独立限流，互不挤占：

```json
[
  {"name": "team-a", "token": "<随机串>", "qps": 2, "daily_quota": 2000},
  {"name": "ci", "token": "<随机串>", "tools": ["web_fetch", "web_search", "web_extract"]}
]
```

- `qps` / `daily_quota` 省略时继承全局 `HTTP_QPS` / `HTTP_DAILY_QUOTA`，写 `0` 表示该租户不限
- `tools` 可选：只允许调用列出的工具，省略或写 `"*"` 表示全部放行；调用白名单外的工具返回 403（`error=tool_forbidden`，并带 `tool` 字段），不会执行任何抓取；批量请求中只要有一个工具越权就整批拒绝
- 租户名与令牌都必须唯一；配置非法（JSON 错误、缺 `name`/`token`、重复、`tools` 非法）会在启动时报错退出
- 与单个 `HTTP_TOKEN`、以及 `HTTP_TOKENS` 与 `HTTP_TOKENS_FILE` 之间均互斥，同时设置会拒绝启动
- 每个租户在 `/metrics` 有独立的 `scrape_mcp_tenant_requests_total{tenant}`、`scrape_mcp_tenant_rejections_total{tenant,reason}` 与 `scrape_mcp_tenant_tool_calls_total{tenant,tool}`；429/403 响应不区分其它租户，不泄露其它调用方信息

看板：`GET /` 是内置运行看板（单文件 HTML，无外部依赖、无 CDN），打开即见请求量、L1/L2 分布、耗时 p50/p95/p99 与分桶、结果码、正文类型，以及 L2 页位与代理池占用，每 5 秒自动刷新。

```bash
scrape-mcp --transport streamable-http --port 8000
# 浏览器打开 http://127.0.0.1:8000/
```

页面本身只是空壳（不含任何抓取数据），因此免鉴权；数据仍走 `/stats` 与 `/metrics` 的鉴权——配置了令牌时页面会先收到 401，填入令牌后存在浏览器 localStorage，之后由页面自动带上。

健康检查：`GET /healthz` 返回版本与关键开关，始终免鉴权，可用于容器探针/网关巡检。

统计：`GET /stats` 返回进程内计数。未配置令牌时只接受回环地址访问（其它来源返回 403）；配置令牌后由 Bearer 鉴权保护，可远程读取：

```bash
curl http://127.0.0.1:8000/stats
# {"ok":true,"uptime_s":42,"requests":7,"fetches":5,"result_cache_hits":2,
#  "http_cache_hits":1,"cache_hit_rate":0.286,"by_tier":{"l1":4,"l2":1},
#  "by_code":{"ok":5},"by_kind":{"html":5},"tokens_returned":8120,"bytes_in":1048576,
#  "latency_ms":{"window":1024,"samples":5,"p50":120,"p95":820,"p99":820,
#   "buckets":{"lt_100":2,"lt_300":1,"lt_1000":1,"lt_3000":1,"lt_10000":0,"gte_10000":0},
#   "by_tier":{"l1":{"samples":4,"p50":90,"p95":260},"l2":{"samples":1,"p50":820,"p95":820}}}}
```

`latency_ms` 是最近 1024 次抓取的耗时分布（分位数 + 固定分桶 + 分层），不含结果缓存命中；样本有上限，长时间运行不会持续占内存。

`by_tier` 是实际生效的层级分布，`by_code` 复用工具返回的错误码（成功为 `ok`），`cache_hit_rate` 是结果缓存命中数 ÷ 总请求数。统计只记计数与字节数，不记 URL、不记页面内容，进程重启后归零。

多令牌模式下 `/stats` 还带 `tenants`：按租户的 `requests`、`tool_calls`（工具分布）、`rejections`（拒绝原因）与 `quota_remaining`（仅设置了日配额的租户）；未配置令牌时为空对象。

指标：`GET /metrics` 输出 Prometheus 文本格式（`version=0.0.4`），访问限制与 `/stats` 完全相同，同样不含 URL 与页面内容：

```bash
curl http://127.0.0.1:8000/metrics
# scrape_mcp_fetches_total 5
# scrape_mcp_fetch_tier_total{tier="l1"} 4
# scrape_mcp_fetch_tier_total{tier="l2"} 1
# scrape_mcp_http_rejections_total{reason="unauthorized"} 0
# scrape_mcp_l2_active_pages 0
```

| 指标 | 类型 | 含义 |
| --- | --- | --- |
| `scrape_mcp_requests_total` / `scrape_mcp_fetches_total` | counter | 总请求数 / 进入抓取链路的次数（后者含 HTTP 缓存命中、不含结果缓存命中） |
| `scrape_mcp_fetch_tier_total{tier}` | counter | 命中层级分布（`l1` = curl_cffi，`l2` = Playwright） |
| `scrape_mcp_fetch_code_total{code}` / `scrape_mcp_fetch_kind_total{kind}` | counter | 按错误码 / 内容形态（`html`/`pdf`/`text`）分布 |
| `scrape_mcp_fetch_duration_seconds` | histogram | 抓取耗时分布（按层级，单位秒；桶 `0.1`/`0.3`/`1`/`3`/`10`/`+Inf`），可直接算 p95 与平均耗时 |
| `scrape_mcp_result_cache_hits_total` / `scrape_mcp_http_cache_hits_total` | counter | 两层缓存命中次数 |
| `scrape_mcp_tokens_returned_total` / `scrape_mcp_bytes_in_total` | counter | 返回 token 与入站字节累计 |
| `scrape_mcp_http_rejections_total{reason}` | counter | HTTP 拒绝次数：`unauthorized` / `forbidden` / `tool_forbidden` / `rate_limited` / `quota_exceeded` |
| `scrape_mcp_tenant_requests_total{tenant}` | counter | 按租户统计的已通过请求数（只含计入配额的请求，`/healthz`、`/metrics` 不计） |
| `scrape_mcp_tenant_rejections_total{tenant,reason}` | counter | 按租户统计的拒绝：429 限流/配额与 403 工具白名单（`rate_limited` / `quota_exceeded` / `tool_forbidden`） |
| `scrape_mcp_tenant_tool_calls_total{tenant,tool}` | counter | 按租户与工具统计的已执行调用次数（通过权限与配额的 `tools/call`） |
| `scrape_mcp_tenant_quota_remaining{tenant}` | gauge | 租户当日剩余配额（只输出设置了 `daily_quota` 的租户，可用来做余量告警） |
| `scrape_mcp_l2_active_pages` / `scrape_mcp_l2_queued_pages` / `scrape_mcp_l2_contexts` | gauge | 当前在途 L2 渲染页数 / 排队等待页位的请求数 / 常驻浏览器上下文数 |
| `scrape_mcp_l2_browser_running` / `scrape_mcp_l2_max_pages` / `scrape_mcp_l2_max_contexts` | gauge | 浏览器是否运行、并发页与常驻 context 上限 |
| `scrape_mcp_proxy_pool_size` / `_healthy` / `_cooling` | gauge | 代理池出口总数 / 未冷却数 / 冷却中数；未配置 `PROXY_POOL` 时不输出这组指标 |

`/metrics` 不额外引入 `prometheus_client`，按文本格式直接拼装。鉴权与 `/stats` 相同，但**不计入 QPS 与日配额**，可放心让 Prometheus 按固定间隔抓取；抓取配置见 [DEPLOYMENT.md](DEPLOYMENT.md#6-健康检查与观测)。

**请求关联 ID**：每次工具调用都会生成一个 12 位 `request_id`，同时出现在返回载荷与 stderr 的 JSON 日志里（字段同名）。`web_batch` / `web_crawl` 的子任务共用外层 ID，因此一次调用只有一个 ID；结果缓存命中时载荷里是本次调用新生成的值，不会沿用首次请求的 ID。报障时提供该 ID 即可按日志定位整条链路。

**链路追踪（可选）**：`SCRAPE_MCP_OTEL_ENABLED=1` + `pip install "mcp-scrape[otel]"` 后，每次工具调用产生 `tool.<name>` span、每次抓取产生 `fetch` span（属性含 `request_id` / `tier` / `status` / `code` / `elapsed_ms`），按 OTLP/HTTP 导出到 `SCRAPE_MCP_OTEL_ENDPOINT`（默认 `http://127.0.0.1:4318`）。默认关闭；未安装可选依赖时只记一条警告并退化为空操作，不影响抓取。

必需项：请求头 `MCP-Method`、`Mcp-Name`；body 内 `params` 需带 `_meta`（`protocolVersion` + `clientCapabilities`）。完整示例见下。

## 接入 MCP 客户端（Claude / Cursor 等）

先 `pip install mcp-scrape`（或用项目 `.venv` 跑 `pip install -e .`），使 `scrape-mcp` 命令全局可用。

**一条命令生成配置（推荐）**

```bash
scrape-mcp init --client cursor --write     # 或 claude / vscode / json
```

- 默认只打印 diff 预览，加 `--write` 才落盘；目标文件里其它服务配置原样保留
- `--client json` 直接打印 JSON（不落盘），适合贴到任意客户端或自定义路径
- 已有同名 `scrape-mcp` 条目时不覆盖，确需更新加 `--force`
- `--scope global|project` 控制写全局还是项目内（`.cursor/mcp.json`、`.vscode/mcp.json`）
- `--env KEY=VALUE` 可把 `SCRAPE_MCP_PROXY`、`SCRAPE_MCP_L2_CHANNEL` 等写进客户端配置
- `--command` 可指定启动命令；缺省优先用已安装的 `scrape-mcp`，找不到则退回当前解释器 + `-m scrape_mcp.server`

生成的结构（Claude / Cursor 用 `mcpServers`，VS Code 用 `servers` 且带 `"type": "stdio"`）：

```json
{
  "mcpServers": {
    "scrape-mcp": {
      "command": "scrape-mcp",
      "args": []
    }
  }
}
```

- 若复用系统浏览器（免下载）：设环境变量 `SCRAPE_MCP_L2_CHANNEL` 为 `chrome` 或 `msedge`。
- 配对成功后重启客户端，即可在会话里直接调用 `web_fetch` / `web_extract` / `web_batch` / `web_crawl` / `web_map` / `web_crawl_start`。
- 想先看工具定义（名称、说明、入参 schema）：`scrape-mcp schema --tool web_fetch`，或 `--output tools.json` 导出全量。
- 没有图形界面（容器/服务器）时不必开浏览器：`scrape-mcp session import --url https://<站> --file cookies.txt` 直接导入浏览器扩展导出的 cookies；`scrape-mcp session list` 查看已保存的登录态。

## MCP 工具

- **`web_fetch(url, max_tokens=4000, link_policy="internal", format="markdown", ...)`** → 状态 + 极省正文
  - `link_policy`: `internal`（站内相对链接+站外降级）/ `all` / `none`
  - `format`: `markdown`（默认，保留标题/列表/链接）/ `text`（剥掉标记的纯文本）/ `json`（`content` 为段落数组）/ `chunks`（RAG 分块，见下）
  - `format=chunks`: `content` 为分块数组，每块含 `index` / `text` / `heading_path`（标题层级路径）/ `start` / `end`（在正文中的字符偏移）/ `tokens`；顶层另有 `chunk_count`，可直接写入向量库
  - `chunk_size` / `chunk_overlap`: `chunks` 模式的窗口与重叠字符数（默认 `1200` / `200`；窗口 100~8000，重叠不超过窗口一半）
  - 抓取参数：`headers` / `cookies`（自定义请求，不走共享缓存）、`timeout`、`bypass_cache`
  - 代理：`proxy`（如 `http://127.0.0.1:7890`、`socks5://127.0.0.1:1080`）只对本次调用生效，L1 与升级后的 L2 走同一个出口；不传则按代理池分配，未配池时沿用 `SCRAPE_MCP_PROXY`。显式传入代理的请求不读也不写共享缓存，避免不同出口的结果混在一起
  - 出口优先级：单请求 `proxy` > 会话绑定（`SCRAPE_MCP_PROXY_SESSIONS`）> 代理池 > 全局 `PROXY`
  - 地区定向：`region`（如 `us` / `hk` / `jp`）只在走代理池时生效，仅从带该地区标签的出口中选择（池条目写成 `http://host:port|us`）；没有该地区出口时直接返回 `invalid_argument`，不会悄悄换出口。会话绑定了代理或显式传了 `proxy` 时不生效，`notes` 会说明实际出口
  - 挑战诊断：被抓页面被判为拦截时，载荷新增 `challenge`（`provider` / `kind` / `sitekey` / `action` / `status` / `retry_after` / `evidence`），正常页面为 `null`。`sitekey` 只回传前 6 位；支持 Cloudflare / Turnstile / DataDome / Akamai / PerimeterX / reCAPTCHA / hCaptcha，无法归类时保持 `null`
  - CAPTCHA 钩子（`SCRAPE_MCP_CAPTCHA_SOLVER_URL`）默认关闭且**不做破解**：仅在检测到人机验证时把 URL/状态码/拦截类型（识别到挑战时附带 `provider` / `kind` / `sitekey` / `action`）转给你指定的合规服务（人工值守或已获授权的 solver），按对方返回的 `cookies` 重试一次（L1 不行再试一次 L2）；失败只写 `notes`，不影响原有结果。使用前请确认目标站条款与当地法律，不要用于大规模绕过验证或批量注册
  - 求解缓存：`SCRAPE_MCP_CAPTCHA_COOKIE_CACHE=true` 时把求解得到的 cookie 按 `(session, host)` 缓存 `CAPTCHA_COOKIE_TTL` 秒，同站点再次被拦先用缓存重试，命中不再调用求解服务。cookie 属敏感数据，多租户共用进程时请谨慎开启
  - 住宅出口升级：配置 `SCRAPE_MCP_PROXY_PROVIDER_TEMPLATE`（或池中 `|residential` 条目）后，完整链路仍命中 `rate_limit` / `cloudflare` / `captcha` / `waf` 且预算足够时，只升级一次住宅出口重试；显式 `proxy` 与会话绑定代理不改写。代理凭据在 `notes` 与日志中脱敏为 `***`
  - 总预算：`deadline_ms`（毫秒，可选）限制整次调用；不够时逐级降级，见"错误码与诊断"中的 `deadline:*` 说明
  - 登录态：`session`（默认 `default`），与 `login` 时的会话名一致即可自动带上对应登录态
  - 渲染参数（仅 L2 生效）：`wait_for`（等 CSS 选择器）、`scroll`（滚动触发懒加载）、`click`（点"加载更多"）；`actions`（动作链，按顺序执行 click/type/press/select/wait/scroll，单个动作失败只写入 `notes`）
  - 非 HTML：PDF 返回结构化文本（装 `[pdf]` extra；`kind="pdf"` 且带 `pages`/`pages_parsed`）；DOCX/XLSX/PPTX 直接用标准库抽取（`kind` 对应 `docx`/`xlsx`/`pptx`）；纯文本原样返回（`kind="text"`）
  - 扫描 PDF：`OCR_ENABLED=1` 且装了 `[ocr]` extra 时对无文字层页面 OCR，载荷带 `ocr=true`；OCR 不可用或仍无文字时返回 `unsupported_content` / `empty`
  - 诊断参数：`debug=true` 时返回 `diagnostics`（决策轨迹、选择器命中数、L2 交互结果），不含页面内容
- **`web_extract(url, schema=None, ...)`** → 按字段 schema 抽取结构化 JSON（可入库）
  - `schema` 可省略：命中站点 recipe 时直接用 recipe 里的 schema，否则返回 `invalid_argument`
  - 字段类型：`text`（默认）/ `html` / `attr`（需配 `attr`）/ `count` / `list` / `object` / `jsonld`（需配 `path`）
  - 嵌套：`list` 配 `fields` 返回对象数组，`object` 在匹配节点内部继续抽取
  - 后处理：`regex` + `group`（正则提取，未命中回落 `default`）、`abs_url`（相对链接转绝对）、`strip`
  - 类型转换：`convert` 可设 `int` / `float` / `date`（去千分位与货币符号；日期兼容 ISO 与 `2026年10月20日` 这类写法）
  - 必填校验：字段设 `required: true`，缺失时整次调用 `ok=false`、`error_code=missing_required`，并在 `required_missing` 列出字段名
  - `jsonld` 用点号路径从 `<script type="application/ld+json">` 取值（多个 script / `@graph` 自动聚合，数组自动展开、数字段视为下标）
  - 插件类型：第三方包可通过入口点注册自己的字段类型（见下文「自定义字段类型」），用法与内置类型一致
  - 抓取/渲染参数与 `web_fetch` 相同（不含 `link_policy`/`format`）；非 HTML（PDF/纯文本）返回 `unsupported_content`，请改用 `web_fetch`
  - `prompt`（可选）：LLM 辅助抽取。配置 `LLM_BASE_URL` + `LLM_MODEL` 后把 compact 正文交给 OpenAI 兼容模型；未配置或调用/解析失败自动回退确定性 schema，**不会产生默认外部调用**
  - 返回 `extract_mode`（`llm` / `schema`）与 `model`；回退原因写在 `notes`。字段规则可用 `description` 说明含义，会随 prompt 一起交给模型
  - 隐私提示：启用 LLM 意味着页面正文会发送到你配置的端点，不要把不可外发的页面交给它
- **`web_batch(urls, max_tokens=3000, link_policy="internal", format="markdown", ...)`** → 并发抓取一批，逐 URL 返回；单条异常不影响整批
  - 支持 `headers` / `cookies` / `timeout` / `bypass_cache` / `debug` / `session`（对该批所有 URL 生效）；`format=chunks` 与 `chunk_size` / `chunk_overlap` 同样逐条生效
  - `proxy` 同样对该批所有 URL 生效；缺省按代理池分配，未配池时沿用全局配置
  - `region` 同样对该批所有 URL 生效（如 `us`）；只在走代理池时生效，无该地区出口时整批返回 `invalid_argument`
  - `deadline_ms` 为整批共用的预算：耗尽后还没开始的子任务直接返回 `timeout`，不再占用网络与浏览器
  - 客户端请求进度（传 `progressToken`）时，每完成一条上报 `已抓取 n/N 个 URL`
- **`web_crawl(url, depth=1, limit=10, same_host=True, max_tokens=2000, ...)`** → 从一个入口逐页抓取，返回带 `mode` 标明模式
  - `mode=links`（默认）：按 `depth`/`limit` 发现站内链接逐层抓取；`depth` 最大 `3`，`limit` 受 `CRAWL_MAX_PAGES` 约束
  - `mode=paginate`：传 `next_selector` 即启用分页模式，按"下一页"链接顺序翻页，`max_pages` 封顶（默认 `5`），已访问 URL 自动去重，默认不跟随站外链接（`same_host=false` 可放开）
  - 单页失败只影响该页；图片/压缩包等静态资源链接自动跳过；`session` 对该次所有页面生效
  - `proxy` 对该次所有页面生效；缺省按代理池分配，未配池时沿用全局配置
  - `region` 对该次所有页面生效（如 `us`）；只在走代理池时生效，无该地区出口时直接返回 `invalid_argument`
  - `deadline_ms` 为整次爬取的预算：耗尽后不再抓新页，已抓到的页面照常返回
  - 客户端请求进度时逐页上报（分页模式为 `已翻到第 n/N 页`，链接模式为 `已抓取 n/N 页`）
- **`web_crawl_start(url, depth=1, limit=10, ..., next_selector=None, max_pages=None, deadline_ms=None, session="default")`** → 启动异步爬取任务，立即返回 `job_id`
  - 抓取语义与 `web_crawl` 相同；任务在后台推进，页数多时不会阻塞 MCP 请求
  - 状态逐页原子落盘到 `DATA_DIR/jobs/<job_id>.json`；同参数重复 start 幂等（`existing=true`）
  - **`web_crawl_status(job_id, include_results=True)`** → 读取状态、进度与已抓页面；进程重启后会从断点续跑
  - **`web_crawl_cancel(job_id)`** → 停止任务，已完成页面的部分结果仍可读取；终态任务幂等返回
  - **`web_crawl_list(limit=20)`** → 列出最近任务（不含正文）
- **`web_watch(url, selector=None, reset=False, bypass_cache=True, ...)`** → 对比页面自上次抓取以来的变化
  - 每次成功抓取都把正文存一份快照到 `DATA_DIR/watch/`，下次调用与它对比：返回 `changed`、`added_lines` / `removed_lines` 与 `diff` 预览（最多 40 行，`diff_truncated` 标明是否截断）
  - `first_seen=true` 表示这是该 URL 的第一份快照（没有可比对象，故不给 diff）；`previous_hash` / `previous_chars` 便于人工核对
  - `selector` 只看页面内某一块（按命中的全部节点拼接），适合盯榜单/价格/公告位；不同 `selector` 各存一份快照，互不覆盖
  - `reset=true` 丢弃旧快照重建基线；比较口径忽略行尾空白与空行，避免渲染噪声误报
  - 抓取失败（拦截/超时/登录墙）不会覆盖快照，恢复后仍能对比出期间变化；`bypass_cache` 默认 `true`，保证拿到的是最新内容
- **`web_schedule(action="list", id=None, kind="watch", url=None, selector=None, interval_s=3600, webhook_url=None, session="default", ...)`** → 管理定时监控任务
  - `create`：登记"盯什么、多久一次、变化后通知谁"；`kind=watch` 复用 `web_watch`（可配 `selector`），`kind=index` 复用 `web_index`；同 `kind+url+selector` 只有一条任务，重复 create 视为更新
  - `interval_s` 允许 60 ~ 604800（一周），默认 3600；`webhook_url` 只接受 http/https
  - `list` / `get` 查看任务与上次状态；`pause` / `resume` 启停（暂停后 daemon 不再执行）；`delete` 删除调度状态（页面快照保留）
  - `run` 立即执行一次：首次返回 `first_seen=true` 且不发通知；之后仅在真实变更时推送 webhook，失败不影响任务状态
  - 实际执行由 `scrape-mcp schedule daemon` 驱动（见下文「定时监控」）；任务状态落在 `DATA_DIR/schedule/tasks.json`
- **`web_search(query, max_results=10, provider=None, site=None, time_range=None, engines=None, fallback=None, ...)`** → 搜索关键词，返回结构化结果列表
  - `provider`: `duckduckgo`（默认，取 `SEARCH_PROVIDER`）/ `searxng`（需先设 `SEARCH_SEARXNG_URL`）/ `brave` / `tavily`（需先设对应 key）
  - `site` 以 `site:` 运算符透传给后端（是否严格限定取决于该引擎支持程度）；`time_range` 取 `day` / `week` / `month` / `year`；`engines` 为 SearXNG 引擎名单（仅对 searxng 生效）
  - 每条结果含 `rank` / `title` / `url` / `snippet` / `engine` / `published_at`；`attempts` 记录回退前的失败后端
  - 后端失败（限流/拦截/传输错误）时按 `fallback`（默认取 `SEARCH_FALLBACK`）自动回退到其他已配置的后端；全部不可用时行为与之前一致
  - 搜索服务按接口使用：仍遵守主机白/黑名单与全局限流，但不套目标站的 robots 抓取规则
  - `bypass_cache` 强制现查；`proxy` 与 `deadline_ms` 语义同 `web_fetch`，回退同样受总预算约束
- **`web_screenshot(url, path=None, full_page=False, width=None, height=None, wait_ms=0, selector=None, hide=None, device=None, pdf=False, ...)`** → 浏览器渲染并截图，返回本地 PNG/PDF 路径
  - 不传 `path` 时存到 `DATA_DIR/screenshots/`；`full_page=true` 截整页，否则只截视口；`pdf=true` 导出整页 PDF（需 Chromium，忽略元素裁剪）
  - `selector` 截图前等待该 CSS 选择器出现，命中时按元素包围盒裁剪；未命中或元素不可见时回退整页并在 `notes` 说明
  - `width`/`height` 不传时取 `device` 预设（desktop 1280x800 / mobile 390x844，mobile 同时用移动 UA 与触屏）；`hide` 在截图前隐藏指定元素（如固定导航/弹窗），单个失败只记 `notes`
  - `wait_ms` 额外等动画/懒加载；`actions` 可在截图前执行点击/输入/滚动等动作
  - 复用 `session` 登录态与浏览器池；`proxy` / `deadline_ms` 语义同 `web_fetch`
- **`web_map(url, include=None, exclude=None, search=None, limit=500, homepage=True, use_search=False, ...)`** → 发现站点 URL（sitemap + 入口页链接）
  - 同一 URL 只出现一次，`urls` 每项带 `source`（`sitemap` / `homepage` / `search`），`sources` 给出各类计数
  - `include`/`exclude` 为正则，`search` 为 URL 子串过滤；`limit` 默认 500、硬上限 5000，超出时 `truncated=true`
  - 无 sitemap 的站点也能从入口页链接得到候选；`use_search=true` 时额外合并 `site:<host>` 搜索结果
- **`web_index(url, include=None, exclude=None, limit=1000, check_content=False, content_diff=False, dry_run=False, reset=False, ...)`** → sitemap 增量索引
  - 传站点根/任意页面 URL 按 host 找 sitemap，也可直接传 sitemap 地址；`include`/`exclude` 用正则过滤 URL
  - 返回 `new` / `changed` / `unchanged` / `removed` 计数与对应 URL 列表（每类最多 50 条），`first_seen` 标明是否首次建立基线
  - `check_content=true` 对新增/变更条目回抓正文比哈希（单次最多 20 个），内容没真变的不算 `changed`
  - `content_diff=true` 在回抓正文的同时与 `web_watch` 共用快照，并额外返回 `changes`：逐 URL 的 `changed` / `first_seen` / `added_lines` / `removed_lines` / `diff`（截断）；快照写回后单页 `web_watch` 可直接接着对比，`dry_run=true` 不写快照
  - `dry_run=true` 只对比不落盘；`reset=true` 丢弃旧基线；`limit` 限制本次处理条数（硬上限 5000）
- **`login(url, timeout=180, session="default", headless=False, qr_selector=None, cdp_url=None)`** → 打开浏览器登录并持久化登录态
  - 多账号场景用不同 `session` 名分别登录，`web_fetch` 时传同一个名字即可，互不覆盖
  - `headless=true`：服务器无 GUI 时不弹窗口，登录页截图保存到 `DATA_DIR/login/`，载荷给出 `screenshot` 路径供查看/扫码
  - `qr_selector`：只截取登录二维码元素（选择器未命中时回退整页并在 `notes` 说明），适合手机扫码
  - `cdp_url`：连接远程浏览器（如另一台机器上的 Chrome）完成登录，适合本地既无 GUI 也不方便扫码的场景
  - 也可以完全绕开浏览器：用 `session_import` 导入浏览器扩展导出的 cookie
- **`session_list()`** → 列出本地已保存的登录态（`session` + `host` + `updated_at`），不含 cookie 内容
- **`session_export(url, session="default", path=None)`** → 导出该站 cookie（`{host, session, cookies}`）；传 `path` 时同时写入本地文件
- **`session_import(url, path, session="default", replace=False)`** → 从本地文件导入 cookie
  - 兼容 `session_export` 产物、Playwright `storage_state`、裸 cookie 数组，以及浏览器扩展导出的 Netscape `cookies.txt`
  - 默认按 `(name, domain, path)` 合并（同名以导入值为准），`replace=true` 整体替换；只读本地文件，不发网络请求

返回为 JSON 字符串（落在 `content[0].text`，请解析文本，勿依赖 `structured_content`）。
失败时带稳定 `error_code` 与 `retryable`；传输失败/5xx 会自动指数退避重试，`attempts` 为实际尝试次数；L2 交互未命中（如选择器不存在）只在 `notes` 中提示，不判失败。

### 错误码与诊断

`error_code` 是协议的一部分（只增不改），`retryable` 直接告诉你"过一会儿重试是否有意义"：

| error_code | retryable | 含义 / 下一步 |
|---|---|---|
| `timeout` / `dns` / `tls` / `connection` / `unknown` | ✅ | 网络层问题，稍后重试或检查代理 |
| `http_5xx` | ✅ | 目标服务端异常，可退避重试 |
| `http_4xx` | ❌ | 404/410 等，页面本身无效 |
| `blocked` | ❌ | 被反爬拦截（Cloudflare/WAF/限流），换时段或人工确认 |
| `login_required` | ❌ | 页面确有登录入口（密码框/登录字样），先调用 `login`；裸 403 拒绝页没有登录入口，只报 `blocked` |
| `denied` / `robots_disallowed` | ❌ | 黑白名单或 robots.txt 拒绝 |
| `missing_required` | ❌ | 必填字段缺失，见 `required_missing`；补齐 schema 或放宽 `required` |
| `unsupported_content` | ❌ | 内容形态不支持当前工具（如对 PDF/Office 用 `web_extract`）；改用 `web_fetch`，PDF 需装 `[pdf]` extra，扫描件还需 `[ocr]` |
| `invalid_url` / `invalid_argument` | ❌ | 参数问题 |

失败时默认带精简 `diagnostics.decisions`（如 `["retry:5xx", "escalate_l2:cloudflare", "l2_ok"]`）；
走粘性升级时首条是 `skip_l1:sticky_l2`，浏览器这次没跑起来则会看到 `sticky_l2_failed:fallback_l1`。
传了 `deadline_ms` 且触发降级时，会看到 `deadline:skip_retry`（预算不够再等一次退避）、
`deadline:skip_l2`（不够启动浏览器，返回 L1 结果）或 `deadline:exceeded`（没来得及发起请求）。
传 `debug=true` 会展开更多计数：

```json
{"diagnostics": {
  "decisions": ["escalate_l2:shell", "l2_ok"],
  "attempts": 1,
  "html_bytes": 348210,
  "root": "div",
  "selector_hits": {"article": 0, "main": 1, "div": 812},
  "content_chars": 5820,
  "l2": {"wait_for_hit": null, "clicked": null, "scroll_rounds": 0, "error": null}
}}
```

429/503 带 `Retry-After` 时返回 `retry_after_s`；配置 `RETRY_AFTER_MAX_WAIT` 后可在上限内自动补一次重试。

### web_extract 示例

```bash
schema='{"fields": {
  "title":     {"selector": "h1", "required": true},
  "price":     {"selector": ".price", "regex": "￥([\\d,.]+)", "group": 1,
                "convert": "float", "required": true},
  "published": {"selector": "time", "type": "attr", "attr": "datetime", "convert": "date"},
  "main_link": {"selector": "a", "type": "attr", "attr": "href", "abs_url": true},
  "tag_count": {"selector": ".tag", "type": "count"},
  "tags":      {"selector": ".tag", "type": "list", "list_key": "text"},
  "author":    {"type": "jsonld", "path": "author.name"},
  "items":     {"selector": ".item", "type": "list", "fields": {
    "name": {"selector": ".name"},
    "url":  {"selector": "a", "type": "attr", "attr": "href", "abs_url": true}
  }}
}}'
# → {"ok":true, "data": {"title":"…","price":299.0,"published":"2026-10-20",
#    "main_link":"https://…","tag_count":3,"tags":["a","b","c"],
#    "author":"…","items":[{"name":"…","url":"https://…"}]},
#    "missing":[], "required_missing":[]}
```

必填字段缺失时返回 `ok=false` + `error_code=missing_required`，`required_missing` 里是缺失的字段名：

```json
{"ok": false, "error_code": "missing_required", "error": "必填字段缺失: title, price",
 "retryable": false, "required_missing": ["title", "price"], "data": {"title": null, ...}}
```

### 站点 recipe

把"这个站怎么抓、取哪块、抽哪些字段、怎么翻页"写成一份 JSON 放进 `RECIPES_DIR`
（默认 `~/.scrape_mcp/recipes`），之后同一站点免参数复用。文件名即主机名，
也可用 `host` 字段覆盖；匹配按最长后缀（`m.example.com` 能命中 `example.com`），
单次调用的显式参数优先于 recipe。

`~/.scrape_mcp/recipes/example.com.json`：

```json
{
  "host": "example.com",
  "wait_for": "main article",
  "scroll": true,
  "root_selector": "main article",
  "extract": {
    "fields": {
      "title": {"selector": "h1", "required": true},
      "author": {"type": "jsonld", "path": "author.name"},
      "published": {"selector": "time", "type": "attr", "attr": "datetime", "convert": "date"},
      "items": {
        "selector": ".item",
        "type": "list",
        "fields": {
          "name": {"selector": ".name"},
          "url": {"selector": "a", "type": "attr", "attr": "href", "abs_url": true}
        }
      }
    }
  },
  "crawl": {"next_selector": "a.next", "max_pages": 5}
}
```

| 字段 | 作用 | 生效范围 |
|---|---|---|
| `host` | 匹配的主机（缺省用文件名） | 全部 |
| `wait_for` / `scroll` / `click` | L2 渲染交互，缺省则用调用参数 | `web_fetch` / `web_extract` / `web_batch` / `web_crawl` |
| `root_selector` | 指定正文根，命中失败自动回落密度选根 | 同上 |
| `extract` | `web_extract` 省略 `schema` 时的默认 schema | `web_extract` |
| `crawl.next_selector` / `crawl.max_pages` | 分页翻页规则，`next_selector` 一旦生效即进入 `mode=paginate` | `web_crawl` |
| `note` | 备注，仅供人读 | — |

改完 JSON 无需重启：按文件 mtime 自动重载。写坏的 JSON 会被跳过，不影响其他 recipe。

#### 开箱示例（真实站点）

`examples/recipes/` 里有八份可直接使用的 recipe，复制到 `RECIPES_DIR` 即可：

```bash
mkdir -p ~/.scrape_mcp/recipes
cp examples/recipes/*.json ~/.scrape_mcp/recipes/
```

| 文件 | 站点 | 演示的 recipe 能力 |
|---|---|---|
| `docs.python.org.json` | Python 官方文档 | `root_selector` 取 `div.body`；`crawl.next_selector` 用 `accesskey="N"` 顺序翻页；`extract` 取标题/版本/章节数/下一章链接 |
| `github.com.json` | GitHub Trending | `type=list` 一次抽整张榜单（仓库名、URL、今日 star、语言、简介） |
| `ruanyifeng.com.json` | 阮一峰的网络日志 | 正文根 `#main-content` + `regex` 提取发布日期；`host` 用 `ruanyifeng.com` 以覆盖 `www.` 子域 |
| `developer.mozilla.org.json` | MDN 文档 | 正文根用 `main#content`（该站没有 `article` 标签）；`extract` 取标题/章节数/正文链接数 |
| `fastapi.tiangolo.com.json` | FastAPI 文档 | Material for MkDocs 站：正文根 `.md-content__inner`，避开 `.md-sidebar` 导航 |
| `cn.vuejs.org.json` | Vue 中文文档 | VitePress 站：正文根 `.vt-doc`（旧版主题是 `.vp-doc`） |
| `cnblogs.com.json` | 博客园首页 | 列表页 `type=list` 一次抽 20 条（标题/URL/作者/摘要/时间）；翻页只有页码链接，不配 `crawl` |
| `chinanews.com.cn.json` | 中国新闻网文章页 | 正文根 `.left_zw`，标题 `required`；时间/来源/作者在 BaiduSpider 隐藏块 |

```bash
# 按 recipe 抽取（不传 schema）
web_extract(url="https://docs.python.org/3/library/asyncio.html")
web_extract(url="https://github.com/trending")
web_extract(url="https://www.ruanyifeng.com/blog/2026/10/weekly-issue-415.html")
web_extract(url="https://developer.mozilla.org/zh-CN/docs/Web/HTTP/Guides/CORS")
web_extract(url="https://fastapi.tiangolo.com/tutorial/")
web_extract(url="https://cn.vuejs.org/guide/introduction.html")
web_extract(url="https://www.cnblogs.com/")
web_extract(url="https://www.chinanews.com.cn/gn/2026/10-21/10718387.shtml")

# 按 recipe 翻页（next_selector 生效即进入 mode=paginate）
web_crawl(url="https://docs.python.org/3/library/asyncio.html", max_pages=3)
```

> 示例选择器于 2026-10 实测通过；站点改版后失效属正常现象，改一行 selector 即可恢复。

#### 校验与共享

写完先本地验一遍，别等运行时才发现选择器写错：

```bash
scrape-mcp recipe validate examples/recipes          # 校验目录（不传路径则校验 RECIPES_DIR）
scrape-mcp recipe validate ~/my-recipe.json          # 或单个文件
scrape-mcp recipe validate examples/recipes --strict # 警告也算失败，适合挂 CI
scrape-mcp recipe list                               # 列出已生效的 recipe：主机、文件、启用项
```

它会指出：JSON/编码问题（含 Windows 记事本写出的 BOM）、未知字段（带「是不是 `wait_for`？」式近似提示）、非法主机名、选择器语法（用运行时同一个解析器验）、`extract` 的 schema 错误，以及同一目录里 host 重复。退出码 0（通过）/ 1（有问题）/ 2（找不到文件），可直接用于流水线。

`examples/recipes/` 就是社区配方集：加一份新配方 = 放一个 `<host>.json` 并用 `recipe validate` 验过；CI 会以 `--strict` 校验这八份，坏配方进不来。

分享或批量迁移时用 bundle：一个文件装多份 recipe，带 schema 版本与导出信息。

```bash
scrape-mcp recipe export --dir examples/recipes -o recipes.bundle.json  # 整个目录打包
scrape-mcp recipe export --dir examples/recipes cnblogs.com github.com  # 只导出指定 host
scrape-mcp recipe import recipes.bundle.json                      # 导入到 RECIPES_DIR
scrape-mcp recipe import recipes.bundle.json --dry-run            # 只打印计划，不写文件
scrape-mcp recipe import recipes.bundle.json --overwrite          # 覆盖同名但内容不同的本地 recipe
```

导出会跳过解析失败或有结构错误的文件并给出警告。导入先整体校验（schema/版本、host 重复、选择器语法、`extract` 规则），有任何错误就一个文件都不写；同名但内容不同的 recipe 默认记为冲突、退出码 1，避免误覆盖本地调整；内容完全相同则跳过。

单次覆盖：显式参数优先于 recipe，例如 `web_fetch(url="...", wait_for=".comments")`。

### 自定义字段类型（插件）

内置七种类型覆盖大多数页面；遇到特殊结构（`meta` 标签、微数据、需要跨节点计算）时，可以写个插件把自己的类型加进 `web_extract`，用法与内置类型完全一致。

最小插件包（本地包或发布到 PyPI 都行）：

```toml
# pyproject.toml
[project]
name = "scrape-mcp-meta"
dependencies = ["mcp-scrape>=0.18"]

[project.entry-points."scrape_mcp.extractors"]
meta_content = "scrape_mcp_meta:meta_content"
```

```python
# scrape_mcp_meta.py
def meta_content(ctx):
    # ctx.nodes     选择器命中的节点列表（可能为空）
    # ctx.rule      原始字段规则，可读自定义键（如 "sep"）
    # ctx.root      整页解析树；ctx.base_url 页面最终地址
    if not ctx.nodes:
        return None
    return (ctx.nodes[0].attributes or {}).get("content")
```

装好后自动发现，直接当类型用：

```json
{"desc": {"selector": "meta[name=description]", "type": "meta_content"}}
```

约定与边界：
- 名字必须是小写标识符，不能占用内置类型名；嵌套 `fields` 只有内置 `object` / `list` 支持
- 返回字符串或列表时，`regex` / `abs_url` / `strip` / `convert` / `required` 照常生效
- 插件抛异常只影响该字段：记进 `errors` 并回落到 `default`，同页其他字段不受牵连；返回空值同样回落 `default`
- 插件是进程内代码，装了什么就跑什么；不想引入第三方代码就别装扩展

## 数据集导出

把每次成功抓取累积成 JSONL，可直接喂给 pandas / DuckDB / 向量库。默认关闭，配一个路径就生效：

```bash
$env:SCRAPE_MCP_DATASET="D:\data\scrapes.jsonl"
# 或写进 .env：SCRAPE_MCP_DATASET=~/datasets/scrapes.jsonl
```

之后 `web_fetch` / `web_batch` / `web_extract` / `web_crawl` 每产生一条成功结果就追加一行；失败、拦截、超时都不记，免得噪声污染数据集：

```json
{"v": 1, "ts": "2026-10-29T12:00:00+00:00", "tool": "fetch", "url": "https://example.com/a", "final_url": "https://example.com/a", "status": 200, "tier": "l1", "title": "示例", "tokens": 1234, "chars": 5678, "content": "正文……", "error_code": null}
```

`web_extract` 的结果额外带 `data`（字段抽取结果）。查看与转换：

```bash
scrape-mcp dataset stats                        # 条数、按工具分布、时间跨度、平均 token/字符
scrape-mcp dataset stats D:\data\scrapes.jsonl  # 也可显式指定文件
scrape-mcp dataset to-parquet                   # 生成同目录 .parquet（需 mcp-scrape[dataset]）
```

边界：
- 只记成功结果；同一 URL 重复抓取会重复记录，去重交给下游（`url` + `ts` 足够）
- 写盘失败只记 warning，不影响抓取本身；导出是旁路能力
- 文件是持续追加，长期运行请自行按天轮转（如 `scrapes-2026-10-29.jsonl`）

### 写入向量库

抓取产物可以直接进 RAG：示例脚本把 JSONL 切块后写入 Chroma 或 Qdrant，metadata 带 `url` / `title` / `tool` / `ts` / `status`，检索命中后能回链原文。

```bash
pip install "mcp-scrape[vector]"                  # chromadb + qdrant-client
python examples/06_vector_store.py D:\data\scrapes.jsonl --dry-run
python examples/06_vector_store.py D:\data\scrapes.jsonl --backend chroma --persist-dir .chroma
python examples/06_vector_store.py D:\data --backend qdrant --qdrant-url http://localhost:6333
```

- 每块约 900 字符、相邻块重叠 120 字符；ID 由 `url + ts + 块序号` 稳定生成，重复导入不会产生重复点
- `--embedder auto`：装了 `sentence-transformers` 就用 `all-MiniLM-L6-v2`，否则退化为无依赖的 hash 向量；后者只够验证链路，别当语义检索质量用
- `--collection` / `--batch` / `--limit` 可调；`--dry-run` 只打印记录数、页面数、块数与向量维度，不连后端

## 定时监控

把 `web_watch` / `web_index` 交给本机 daemon 定时执行，变化后推送 webhook。任务只存在本地，不引入常驻云服务：

```bash
scrape-mcp schedule add --url https://example.com/news --selector "main .list" --interval 3600 \
  --webhook https://hooks.example.com/scrape
scrape-mcp schedule add --kind index --url https://example.com --interval 86400 \
  --webhook https://hooks.example.com/sitemap
scrape-mcp schedule list          # 查看 ID、状态、下次/上次执行
scrape-mcp schedule run <ID>      # 手动跑一次（首次只建基线）
scrape-mcp schedule pause <ID>    # 暂停，保留快照
scrape-mcp schedule resume <ID>
scrape-mcp schedule delete <ID>
scrape-mcp schedule daemon        # 常驻：每 30 秒扫描到期任务；Ctrl+C 退出
scrape-mcp schedule daemon --once # 只跑当前到期任务（适合系统 cron）
```

- 首次执行只建立基线，不算变更、不发通知；之后 watch 比对正文哈希，index 比对 sitemap 清单的 `new` + `changed`。
- webhook 为 JSON POST：`{"event":"change","task_id","kind","url","selector","checked_at","summary"}`；`summary` 带 diff 摘要或新增/变更 URL 列表。
- webhook 失败只体现在本次执行结果里，不改写任务状态；下一次仍会照常检测。
- 想复用系统 cron 时，把 `schedule daemon --once` 放进 crontab 即可，不必常驻进程。
- 任务状态在 `DATA_DIR/schedule/tasks.json`；页面快照仍在 `DATA_DIR/watch/`、`DATA_DIR/index/`，删除任务不会丢快照。

## 分布式执行（可选）

默认仍是单进程 + 本地 JSON。需要多实例横向扩容时，把异步 crawl 任务放进 Redis 队列：API 进程只投递任务，`scrape-mcp worker` 进程消费并推进，状态对所有实例共享。

```bash
pip install 'mcp-scrape[redis]'
export SCRAPE_MCP_JOB_BACKEND=redis
export SCRAPE_MCP_REDIS_URL=redis://127.0.0.1:6379/0

scrape-mcp --transport streamable-http --port 8000   # API 实例，可起多个
scrape-mcp worker                                    # 执行实例，可起多个
```

- 任务状态存 `<PREFIX>:job:<id>`（`JOB_TTL` 默认保留 7 天），待执行队列为 `<PREFIX>:jobs:queue`；`web_crawl_list` 通过 `<PREFIX>:jobs:index` 按更新时间倒序读取。
- 同参数重复 `web_crawl_start` 仍然幂等，只入队一次；`status` / `cancel` / `list` 读同一份共享状态。取消由 API 进程写入，worker 在下一页开始前读到后停止。
- worker 启动时会把 `running` 且超过 `JOB_STALE_AFTER` 秒未更新的任务重新入队，进程崩溃后不必手动清理；正常推进的任务每页都会刷新时间戳，不会被误抢。
- 未安装可选依赖或连不上 Redis 时，启动会给出明确错误；`JOB_BACKEND=local` 的行为与之前完全一致。
- 注意：当前共享的只有任务状态与队列。HTTP 令牌的 QPS / 日配额计数仍在各进程内存中，多实例部署时需按实例数调整阈值。

## 远程浏览器池（可选）

L2 渲染可以不自启 Chromium，而是连接外部 CDP 服务（Browserless、自建 Playwright 集群，或云厂商的浏览器池）：

```bash
export SCRAPE_MCP_REMOTE_BROWSER_CDP_URL=http://127.0.0.1:3000   # Browserless 示例端点
# 服务需要鉴权时，把 token 拼进 URL 查询串，如 ...?token=xxx
```

- 连接成功时不再本地启动浏览器；空闲回收仍按 `L2_IDLE_TIMEOUT` 断开远程连接。
- 连接失败默认回退本地渠道；`REMOTE_BROWSER_FALLBACK=false` 时直接报错，避免云端池不可用时悄悄换成本地出口。
- 只改变 L2 的浏览器来源，L1 抓取与 `login` 的 `cdp_url` 参数不受影响。
- 远程模式连接的是服务端已有浏览器，`L2_CHANNEL` / `L2_HEADLESS` 等本地启动参数不生效；启动时会校验 URL 协议。

## 云端控制面 broker（可选）

P2-4b 只实现**控制面**：把运维提供的浏览器 / 代理上游按租户分发成短租约，并做并发上限与按日配额；不转发流量、不托管浏览器进程、也不生成计费账单。默认关闭，纯本机或自建部署不受影响。

```bash
export SCRAPE_MCP_CLOUD_BROKER_ENABLED=1
export SCRAPE_MCP_CLOUD_BROWSER_UPSTREAMS=http://browser-pool-1:3000,ws://browser-pool-2:3000
export SCRAPE_MCP_CLOUD_PROXY_UPSTREAMS=socks5://proxy-us:1080|us,http://proxy-hk:8080|hk
export SCRAPE_MCP_CLOUD_LEASE_TTL=300
export SCRAPE_MCP_CLOUD_MAX_LEASES_PER_TENANT=2
export SCRAPE_MCP_CLOUD_DAILY_LEASES=100
```

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/cloud/browser/lease
# {"ok":true,"lease_id":"...","kind":"browser","url":"http://browser-pool-1:3000",
#  "region":"","expires_at":"...Z","expires_in_s":300,"request_id":"..."}

curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/cloud/usage
# {"ok":true,"tenant":"team-a","lease_ttl_s":300,
#  "browser":{"active":1,"limit":2,"daily_used":1,"daily_limit":100,"daily_remaining":99,"upstreams":2},
#  "proxy":{...},"request_id":"..."}

curl -X POST -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8000/cloud/proxy/lease?region=us"   # 只从 us 上游分配

curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/cloud/leases
# {"ok":true,"tenant":"team-a","count":2,"leases":[...],"request_id":"..."}

curl -X DELETE -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8000/cloud/proxy/lease/<lease_id>"  # 提前归还
```

- `POST /cloud/<kind>/lease` 申请新租约；`GET` 复用当前未过期租约（没有才发放，复用不消耗日配额）。`kind` 为 `browser` 或 `proxy`
- 代理租约可带 `?region=us`，只从对应地区标签的上游分配，轮询游标按「类型 + 地区」独立；未知地区返回 503 `cloud_unavailable`，浏览器带 `region` 或标签格式非法返回 400 `cloud_bad_request`
- 上游按轮询分配；租约到期自动回收。浏览器上游是 CDP 端点，代理上游条目可带 `|region` 标签
- `DELETE /cloud/<kind>/lease/<lease_id>` 提前归还租约：立即空出并发名额，但不退回当日配额；不存在、已过期或不属于当前租户返回 404 `cloud_lease_not_found`
- `GET /cloud/leases` 列出本租户全部活跃租约（浏览器 + 代理，按到期时间升序）
- 单租户并发租约达到 `CLOUD_MAX_LEASES_PER_TENANT` 时返回 429 `cloud_lease_limit`，带 `Retry-After` 与 `retry_after_s`
- 日租约配额（`CLOUD_DAILY_LEASES`，UTC 零点重置，浏览器/代理分别计数，与 `HTTP_DAILY_QUOTA` 独立）超限返回 429 `cloud_quota_exceeded`
- `/cloud/*` 未启用返回 404 `cloud_disabled`；启用但某类上游为空返回 503 `cloud_unavailable`；租户身份来自 Bearer 中间件，未配置令牌的纯本机模式记为 `local`
- 上游地址在启动时校验（浏览器 `http/https/ws/wss`，代理 `http/https/socks4/socks5` 且地区标签合法），非法配置直接拒绝启动
- 租约与配额默认保存在进程内；设置 `CLOUD_BROKER_REDIS_URL` 后改存 Redis，多实例共享轮询游标、并发上限与日配额，写路径按「租户 + 类型」加短锁避免同时击穿；需 `pip install "mcp-scrape[redis]"`，启动时会检查连接

## 快照对象存储（可选）

默认快照仍写 `DATA_DIR/watch`。设置 `SNAPSHOT_BACKEND=s3` 后，`web_watch` 与 `web_index(content_diff)` 共用同一份快照，落到 S3 兼容存储，多个实例共享基线。

```bash
pip install 'mcp-scrape[s3]'
export SCRAPE_MCP_SNAPSHOT_BACKEND=s3
export SCRAPE_MCP_S3_BUCKET=scrape-snapshots
export SCRAPE_MCP_S3_ENDPOINT_URL=http://127.0.0.1:9000   # MinIO 示例
export SCRAPE_MCP_S3_ACCESS_KEY_ID=...
export SCRAPE_MCP_S3_SECRET_ACCESS_KEY=...
export SCRAPE_MCP_S3_PATH_STYLE=true
```

- 对象键为 `<S3_PREFIX>/watch/<hash>.json`，内容与本地快照一致（含 `saved_at`）。
- 快照是旁路：上传或读取失败都按“没有快照”处理，不影响本次抓取；首次写入即建立基线。
- 默认仍是 `local`；`SNAPSHOT_BACKEND=s3` 未设置 `S3_BUCKET` 或未安装 boto3 时启动直接报错。
- 凭据留空时走 boto3 默认链（环境变量 / IAM 角色），也适合用预签名或实例角色免密部署。

## 数据集与任务产物归档（可选）

同样复用上面的 `S3_*` 配置，另有两处旁路：

- **数据集**：`DATASET_BACKEND=s3` 时不需要设置 `DATASET` 路径，每条成功结果写成一个对象 `<S3_PREFIX>/dataset/<UTC 日期>/<uuid>.json`，多实例并发不会互相覆盖。`scrape-mcp dataset stats` 直接从 S3 汇总；`dataset to-parquet` 需用 `-o/--out` 指定本地输出路径。
- **任务产物**：`S3_JOB_ARTIFACTS=1` 时，异步 crawl 进入终态（done / failed / cancelled）会把完整任务 JSON 归档到 `<S3_PREFIX>/jobs/<job_id>.json`，便于审计与离线分析。
- 两处都是旁路：上传失败不影响抓取结果或任务状态；任一启用（或快照用 s3）都会在启动时检查 `S3_BUCKET` 与 boto3 依赖。

## 生态集成（LangChain / LlamaIndex）

适配器是薄封装：内部调用 `scrape_mcp.sdk`，参数、JSON 载荷与错误码和 MCP 工具完全一致。核心包不依赖这两个框架，只有显式安装可选依赖后才可用。

### LangChain

```bash
pip install 'mcp-scrape[langchain]'
```

```python
from scrape_mcp.integrations.langchain import build_tools

tools = build_tools()  # scrape_web_fetch / scrape_web_extract / scrape_web_search
# 交给 create_tool_calling_agent(...) 或任意支持 tool calling 的 Agent
```

工具返回 JSON 字符串；`scrape_web_extract` 的字段选择器在模型侧仍叫 `schema`。

### LlamaIndex

```bash
pip install 'mcp-scrape[llamaindex]'
```

```python
from scrape_mcp.integrations.llamaindex import build_reader

reader = build_reader()
docs = reader.load_data(["https://example.com", "https://docs.python.org/3/"])  # 同步
# 已在事件循环内时改用：docs = await reader.aload_data([...])
```

- `load_data()` 在没有运行事件循环时内部用 `asyncio.run`；在协程里会提示改用 `await reader.aload_data(...)`。
- 失败页直接跳过；成功的 `content` 成为 `Document.text`，`url/title/status_code/content_type` 进 `metadata`。
- 其余参数（如 `max_tokens`）原样透传给 `web_fetch`。
- 适配器复用进程内的同一个抓取器；脚本退出前可 `await scrape_mcp.sdk.aclose()` 释放浏览器。

## Python SDK

不接 MCP 也能直接用同一套实现（参数、返回值、错误码与工具完全一致）：

```python
import asyncio

from scrape_mcp.sdk import (
    aclose,
    batch,
    crawl,
    crawl_cancel,
    crawl_start,
    crawl_status,
    extract,
    fetch,
    index,
    screenshot,
    search,
    site_map,
)


async def main() -> None:
    page = await fetch("https://docs.python.org/3/library/asyncio.html", max_tokens=2000)
    print(page["ok"], page["token_estimate"], page["tier"])

    # 按字段抽取；不传 schema 时套用该站 recipe
    data = await extract(
        "https://docs.python.org/3/library/asyncio.html",
        {
            "fields": {
                "title": {"selector": "h1"},
                "sections": {"selector": "section", "type": "count"},
            }
        },
    )
    print(data["data"])

    pages = await crawl(
        "https://docs.python.org/3/library/asyncio.html",
        next_selector='div.related a[accesskey="N"]',
        max_pages=3,
    )
    print(pages["mode"], pages["pages_fetched"])

    many = await batch(["https://example.com", "https://example.org"])
    print(many["ok_count"], "/", many["total"])

    hits = await search("python asyncio 文档", max_results=5)
    print(hits["count"], hits["results"][0]["url"])

    shot = await screenshot("https://example.com", full_page=True)
    print(shot["file"], shot["bytes"])

    urls = await site_map("https://docs.python.org", limit=100)
    print(urls["count"], urls["sources"])

    job = await crawl_start("https://docs.python.org", depth=1, limit=20)
    status = await crawl_status(job["job_id"])
    print(status["state"], status["progress"])

    changes = await index("https://docs.python.org", dry_run=True)
    print(changes["new"], changes["changed"], changes["removed"])

    await aclose()  # 触发过 L2 时释放浏览器子进程；不调用也能跑，只是退出时可能有清理告警


asyncio.run(main())
```

- 返回的就是 MCP 工具的 JSON 载荷（已解析为 dict），失败同样带 `error_code` / `retryable`
- SDK 放在 `scrape_mcp.sdk`，是为了不和内部 `scrape_mcp.extract` 子包重名

## 示例脚本

`examples/` 下按场景提供 5 个可直接运行的脚本（默认参数就能跑通，需要代理时先设 `SCRAPE_MCP_PROXY`）：

| 脚本 | 场景 | 常用参数 |
| --- | --- | --- |
| [01_fetch_article.py](examples/01_fetch_article.py) | 长文压成 token 预算内的正文 | `--max-tokens` `--out` |
| [02_extract_list.py](examples/02_extract_list.py) | 列表页抽结构化数据（不取整页正文） | `--json` `--csv` |
| [03_crawl_docs.py](examples/03_crawl_docs.py) | 按"下一页"翻页并合并成一份 markdown | `--selector` `--max-pages` `--out` |
| [04_pdf_to_text.py](examples/04_pdf_to_text.py) | PDF 直接读成文本（需 `mcp-scrape[pdf]`） | `--out` `--max-tokens` |
| [05_batch_check.py](examples/05_batch_check.py) | 批量巡检，输出层级/状态/token/错误码，失败即非零退出 | 位置参数传 URL |
| [06_vector_store.py](examples/06_vector_store.py) | dataset JSONL 切块写入 Chroma/Qdrant（需 `mcp-scrape[vector]`） | `--backend` `--dry-run` `--collection` |

```bash
python examples/01_fetch_article.py https://ruanyifeng.com/blog/ --max-tokens 1200
python examples/02_extract_list.py --csv trending.csv
python examples/03_crawl_docs.py --max-pages 5 --out docs.md
```

另有 `examples/example_client.py`（MCP 客户端调用示例）与 `examples/recipes/*.json`（站点 recipe）。

## 基准

三类基准，口径不同：`bench.py` 看缓存带来的延迟收益，`bench_sites.py` 看真实站点上取到的内容对不对、全不全，`benchmarks/bench_core.py` 看本地热路径吞吐有没有退化。

### 缓存与延迟

```bash
python scripts/bench.py                              # 冷/热对照
python scripts/bench.py --l2 https://news.qq.com/    # 额外看浏览器开销
```

"冷" = `bypass_cache=true`（走网络 + 精简），"热" = 普通调用（命中正文缓存）。
2026-10 实测（Windows 11 / 家宽直连，`rounds=5`，`max_tokens=4000`）：

| 页面 | 层级 | 冷 p50 | 热 p50 | 提升 | token |
| --- | --- | --- | --- | --- | --- |
| example.com | L2 | 495 ms | 1.4 ms | ~360× | 330 |
| docs.python.org（asyncio-task） | L1 | 681 ms | 1.0 ms | ~650× | 3588 |

L2 浏览器：进程内首次调用 ≈3.5 s（含启动），复用 p50 ≈1.8 s。
网络与目标站状态会明显影响绝对值，重点看同一台机器上的冷热对比。

### 真实站点质量

```bash
python scripts/bench_sites.py                          # 全部站点
python scripts/bench_sites.py --nightly                # 夜间回归子集
python scripts/bench_sites.py --category docs,list     # 按分类过滤
python scripts/bench_sites.py --json out.json --md out.md
python scripts/bench_sites.py --baseline out.json      # 与上次结果对比
python scripts/bench_sites.py --nightly --baseline benchmarks/baseline.json  # 与仓库基线对比
pip install trafilatura                                # 可选：对照第三方正文抽取器
python scripts/bench_sites.py --reference trafilatura
```

站点集在 [benchmarks/sites.json](benchmarks/sites.json)，按文档 / 博客 / 资讯 / 列表 / 前端渲染 / 登录墙与反爬 / PDF 分层，每站可配置：

- `expect`：`ok` 表示应取到正文；`login_required` / `robots_disallowed` 等错误码表示该站应被如实拦截，而不是伪装成功；也可以写成列表（如 `["ok", "login_required"]`），表示不同出口下结果不同、任一都算符合预期
- `min_chars`：正文字符数下限
- `checks`：正文中必须出现的稳定片段，用于衡量完整度（只给结构稳定的页面配置，首页类动态内容不配）

输出包含通过率、取正文率、拦截符合率、完整度、token、p50/p95 延迟与分层分布，可写 JSON / Markdown 留档。
`--baseline` 会按站点对比上次结果，并把正文字符下降超过 20% 的站点标为退化。
同一份基线还用于耗时门禁：某站耗时超过 `max(基线 × 3, 基线 + 3000ms)` 即判为退化、退出码非零，夜间工作流据此开 issue；两个阈值可用 `--latency-factor` / `--latency-floor-ms` 调整。

仓库里保留一份夜间子集基线 [benchmarks/baseline.json](benchmarks/baseline.json)，2026-10-28 随扩容重录（14/14 通过，p50 504 ms / p95 1642 ms）。它只覆盖夜间子集，用于看趋势，不代表全量站点通过率；每月挑一次夜间结果更新：下载当次 `bench-sites.json`，把 `sites_file` 改为相对路径、补上 `source` 字段后替换该文件。

**本机实测（2026-10-28，37 站）**：通过 33/37，完整度 33/33，拦截符合 4/4，延迟 p50 1705 ms / p95 9011 ms。
未通过的 4 项里，3 项是本机网络不可达（Hacker News / V2EX / arXiv 超时），1 项被站点拦截（Bilibili 返回 `login_required`）；
这些站点在 GitHub 网络的夜间任务里会重新跑，结果以 `site-quality-<run_id>` 工件为准。
CI 全量（2026-10-28）：通过 37/37，取正文 32/33，拦截符合 4/4，完整度 33/33，延迟 p50 1456 ms / p95 4953 ms。
其中澎湃首页在 GitHub Actions 出口被站点 WAF 恒定 403（三次全量运行都是 `status=403`、`blocked=waf`、0 字），而同站的文章页 `thepaper-article` 正常取到 2827 字，故首页按「如实上报拦截」计入，期望值写为 `["ok", "blocked", "login_required"]`：两种出口都不算退化，正文质量由文章页覆盖。
CI 夜间子集（2026-10-28，14 站）：通过 14/14，取正文 13/13，拦截符合 1/1，完整度 16/16，p50 504 ms / p95 1642 ms。

| 站点 | 分类 | 层级 | 结果 | 字符 | token | 完整度 | 耗时 |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: |
| py-asyncio | docs | l1 | 通过 | 31049 | 7222 | 2/2 | 2001 ms |
| mdn-fetch | docs | l1 | 通过 | 2639 | 1110 | 2/2 | 2975 ms |
| fastapi-first-steps | docs | l1 | 通过 | 8777 | 4124 | 2/2 | 1705 ms |
| vue-intro | docs | l1 | 通过 | 4833 | 3101 | 2/2 | 2479 ms |
| py-json | docs | l1 | 通过 | 27368 | 6903 | 2/2 | 256 ms |
| django-tutorial | docs | l1 | 通过 | 8026 | 4243 | 2/2 | 2318 ms |
| rust-ownership | docs | l1 | 通过 | 24033 | 5740 | 2/2 | 1717 ms |
| ruanyifeng-blog | blog | l1 | 通过 | 908 | 543 | 2/2 | 2432 ms |
| cnblogs-home | blog | l1 | 通过 | 5135 | 3976 | - | 218 ms |
| sspai-home | blog | l1 | 通过 | 1613 | 1285 | 2/2 | 511 ms |
| solidot-home | blog | l1 | 通过 | 8862 | 7166 | 2/2 | 553 ms |
| cnblogs-article | blog | l1 | 通过 | 5883 | 5267 | - | 75 ms |
| meituan-tech | blog | l1 | 通过 | 10561 | 7472 | 1/1 | 1611 ms |
| chinanews-home | news | l1 | 通过 | 935 | 207 | - | 149 ms |
| tencent-news-home | news | l2 | 通过 | 2022 | 1378 | - | 4893 ms |
| thepaper-home | news | l2 | 通过 | 2025 | 1649 | - | 11940 ms |
| 36kr-home | news | l2 | 通过 | 1119 | 1060 | - | 2437 ms |
| thepaper-article | news | l2 | 通过 | 2829 | 3090 | 1/1 | 1520 ms |
| netease-home | news | l1 | 通过 | 10485 | 7313 | 1/1 | 406 ms |
| sina-home | news | l1 | 通过 | 6360 | 7549 | 1/1 | 330 ms |
| github-trending | list | l1 | 通过 | 4913 | 1787 | - | 3682 ms |
| hacker-news | list | l1 | timeout（本机网络） | 0 | 0 | - | 45005 ms |
| v2ex-home | list | l1 | timeout（本机网络） | 0 | 0 | - | 45020 ms |
| stackoverflow-questions | list | l2 | 按预期拦截 | 0 | 0 | - | 11110 ms |
| douban-home | list | l1 | 通过 | 1354 | 1028 | 1/1 | 330 ms |
| react-learn | spa | l1 | 通过 | 13986 | 3514 | 2/2 | 597 ms |
| apple-cn-home | spa | l2 | 通过 | 198 | 90 | 2/2 | 1564 ms |
| svelte-dev | spa | l2 | 通过 | 2320 | 620 | 1/1 | 5797 ms |
| tailwind-home | spa | l1 | 通过 | 6240 | 1778 | 1/1 | 3773 ms |
| zhihu-question | anti_bot | - | 按预期拦截 | 0 | 0 | - | 107 ms |
| xiaohongshu-explore | anti_bot | - | 按预期拦截 | 0 | 0 | - | 76 ms |
| weibo-home | anti_bot | - | 按预期拦截 | 0 | 0 | - | 106 ms |
| bilibili-home | anti_bot | l2 | login_required（站点拦截） | 0 | 0 | - | 2919 ms |
| taobao-home | anti_bot | l2 | 通过 | 5234 | 7563 | - | 2472 ms |
| arxiv-attention | pdf | l1 | timeout（本机网络） | 0 | 0 | - | 45007 ms |
| w3c-dummy-pdf | pdf | l1 | 通过 | 14 | 3 | 1/1 | 1333 ms |
| pdf-orimi | pdf | l1 | 通过 | 391 | 103 | 1/1 | 9011 ms |

`--reference trafilatura` 的对照只统计纯正文；本工具输出保留标题、列表与链接结构，token 数不是等价口径，只看量级即可。

### 核心路径吞吐（CI 门禁）

`benchmarks/bench_core.py` 用固定种子的合成页面（约 370 KB HTML）测不依赖网络的本地热路径，CI 每次 push / PR 都跑一遍，低于 [benchmarks/core_baseline.json](benchmarks/core_baseline.json) 的 ops/sec 下限即失败：

```bash
python benchmarks/bench_core.py            # 打印表格
python benchmarks/bench_core.py --check    # 回归门禁（CI 用法）
python benchmarks/bench_core.py --json out.json
python benchmarks/bench_core.py --update-baseline   # 全量测量后重建下限（需人工确认）
```

| 指标 | 路径 | 参考机实测 |
| --- | --- | ---: |
| `compact_html` | HTML → Markdown（解析 + 密度选正文 + 渲染） | ~18 ops/s（~56 ms/次） |
| `chunk_markdown` | Markdown → RAG 分块（标题路径 + 窗口 + token 计数） | ~18 ops/s（~56 ms/次） |
| `build_record` | 工具结果 → 数据集记录（纯内存） | ~44 万 ops/s |
| `record_result` | 记录 → JSONL 追加写（含锁与磁盘） | ~600 ops/s（~1.7 ms/次） |
| `recipe_validate` | 示例配方校验（8 份） | ~115 ops/s |

参考机为 Windows 11 / Python 3.13；下限取实测的 35%（`build_record` 这种微秒级纯内存操作取 1/10，抖动更大），只拦数量级退化（例如误引入 O(n²)），不做跨机器绝对耗时比较，因此 CI 机器慢一倍也不会误报。

## Docker

```bash
# 预构建镜像（GHCR：main 推 latest，v* 标签推语义化版本）
docker pull ghcr.io/guanyuyan/scrape-mcp:latest

# 或者在本地构建；带 L2 真浏览器（Cloudflare/验证码/SPA）时把 INSTALL_BROWSER 设为 1
docker build -t scrape-mcp .
docker build --build-arg INSTALL_BROWSER=1 -t scrape-mcp:l2 .

docker run -d --name scrape-mcp -p 127.0.0.1:8002:8002 \
  -e SCRAPE_MCP_HTTP_TOKEN=<令牌> \
  -v scrape_data:/data \
  scrape-mcp
curl http://127.0.0.1:8002/healthz     # {"ok":true,"version":"…"}
```

也可以直接 `docker compose up -d --build`（在 `.env` 里设置 `SCRAPE_MCP_HTTP_TOKEN`）。

- 镜像多阶段构建，运行阶段以非 root（uid 10001）启动；`/data` 卷保存登录态与两层缓存
- 容器绑定 `0.0.0.0`，没有令牌会拒绝启动；`/healthz` 免鉴权，供 HEALTHCHECK 使用
- 反向代理、容量规划、升级与威胁模型见 [DEPLOYMENT.md](DEPLOYMENT.md)
- 多实例 + Redis 共享云端租约的参考编排见 [deploy/cloud](deploy/cloud/)（nginx + 两个实例 + Redis），完整步骤见 DEPLOYMENT.md

## 目录结构

```
benchmarks/
├── sites.json         # 真实站点质量基准站点集（分层、期望结果、完整性检查项）
├── baseline.json      # 夜间子集基线（趋势参照，每月更新）
├── bench_core.py      # 本地热路径吞吐基准（固定语料，CI 门禁）
└── core_baseline.json # 核心路径 ops/sec 下限（参考机实测 × 35%，微秒级指标取 1/10）
src/scrape_mcp
├── server.py          # MCP 入口 + 工具（web_fetch/web_extract/web_batch/web_crawl/web_map/web_crawl_*/web_schedule/login/session_*）
├── http_auth.py       # HTTP Bearer 鉴权中间件 + 非回环绑定校验
├── cli.py             # 子命令：init（客户端配置）/ schema（导出工具定义）/ schedule（定时任务 daemon）
├── sdk.py             # Python SDK 薄封装（fetch/extract/crawl/crawl_*/site_map/batch/watch/aclose）
├── integrations/      # LangChain / LlamaIndex 适配（可选依赖）
├── config.py          # 环境变量配置
├── metrics.py         # Prometheus 文本格式指标（/metrics 端点）
├── tokenizer.py       # tiktoken 计数 + 预算截断
├── core/
│   ├── http_client.py # L1 curl_cffi + 磁盘缓存
│   ├── fetcher.py     # 分级调度（L1→L2），对工具层返回 FetchOutcome
│   ├── detector.py    # 拦截/骨架页判定
│   ├── browser.py     # L2 Playwright + stealth
│   ├── session.py     # 登录态持久化（多会话 + cookie 导入导出）
│   ├── recipes.py     # 站点 recipe（按 host 最长后缀匹配、mtime 重载）
│   ├── recipe_bundle.py # recipe 导出/导入 bundle（schema 版本、整体校验）
│   ├── cache.py       # HTTP 响应缓存 + 正文/抽取结果缓存
│   ├── stats.py       # 进程内计数（/stats 端点数据源）
│   ├── jobs.py        # 异步爬取任务（状态落盘、进度、恢复与取消）
│   ├── jobstore_redis.py  # 可选 Redis 后端（共享任务状态与队列，供 worker 横向扩容）
│   ├── watch.py       # 变更检测（正文快照 + diff 摘要）
│   ├── objectstore.py  # 快照存储后端（本地 / S3 兼容）
│   ├── schedule.py    # 定时任务（状态落盘、到期计算、启停）
│   ├── errors.py      # 统一错误码
│   └── hostlimit.py   # 同域并发闸门
└── extract/
    ├── compact.py     # DOM 剪枝 → 极省 token 正文
    ├── schema.py      # 结构化抽取（按字段 schema 出 JSON）
    ├── links.py       # 链接发现（web_crawl 用）
    ├── pdf.py         # PDF → 文本（可选依赖 pypdf）
    ├── documents.py   # DOCX/XLSX/PPTX → 文本（标准库，无额外依赖）
    ├── ocr.py         # 扫描 PDF 的 OCR（可选依赖 pymupdf + pytesseract）
    └── render.py      # markdown → 纯文本 / 分块
```

## 开发

```bash
pip install -e ".[dev]"
python -m pytest
```