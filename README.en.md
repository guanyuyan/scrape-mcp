# scrape-mcp

> **⚠️ This is the official upstream repository for scrape-mcp.** If you find a project with the same or a similar name published under a different author, it is a repost or a derivative, not a release from the original author.

> **An anti-bot-aware MCP server that turns real web pages into clean content with 1%–5% of the tokens.**
>
> Three-tier fetching: `curl_cffi` (TLS/JA3 + HTTP2 impersonation) handles most pages cheaply; only when Cloudflare, captchas or WAFs show up does it escalate to a real Playwright browser with stealth. For login-walled sites, log in once in a visible window, persist the session, and later fetches reuse it automatically — with a clear prompt when it expires.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/mcp-scrape?color=blue&label=pypi)](https://pypi.org/project/mcp-scrape/)
[![Python](https://img.shields.io/pypi/pyversions/mcp-scrape)](https://pypi.org/project/mcp-scrape/)
[![Release](https://img.shields.io/github/v/release/guanyuyan/scrape-mcp?color=blue&label=release)](https://github.com/guanyuyan/scrape-mcp/releases)
[![CI](https://github.com/guanyuyan/scrape-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/guanyuyan/scrape-mcp/actions)

English | [简体中文](README.md)

---

## Quick start (60 seconds)

**Option A: install from PyPI (recommended, zero config)**

```bash
pip install mcp-scrape
# or from source: cd scrape-mcp && pip install -e .
```

Then, from any directory:

```bash
scrape-mcp --version          # confirm the install (prints 0.x)
scrape-mcp init --client claude --write   # wire it into your MCP client
```

> No cloning, no `PYTHONPATH`, no `python -m`. The `scrape-mcp` command works from any
> directory and defaults to stdio so an MCP client can take it over directly.
> For plain HTTP (Postman/curl), run `scrape-mcp --transport streamable-http --port 8000`.

**Option B: run the demo** (fetch + extract against real sites and see the token savings)

```bash
git clone https://github.com/guanyuyan/scrape-mcp.git
cd scrape-mcp
python -m venv .venv && .venv\Scripts\pip install -e .
.venv\Scripts\python scripts\demo.py
```

Typical output (the tier is chosen per site automatically):

```
=== docs | https://developer.mozilla.org/zh-CN/docs/Web/HTTP/CORS
  ok=True tier=l1 status=200 html=260128B → tokens=3730 (~5.7% of the page)
  title: Cross-Origin Resource Sharing (CORS) - HTTP | MDN
  ok=True tier=l1 data={"heading": "Cross-Origin Resource Sharing (CORS)", "h2_count": 10, "links": 665}
```

> An MDN page is ~260 KB of HTML; a few thousand tokens is enough to keep the clean body.
> Blocked or login-walled pages are escalated or reported as such — never passed off as content.

---

## Why it exists

When a model reads a web page, it usually reads the whole DOM — scripts, navigation, footers,
link farms — which is both expensive and noisy. This server trims the page down to the body first.

**Three core properties**

1. **Far fewer tokens** — attribute stripping, boilerplate removal, off-site link demotion, tables to CSV, and budget truncation. Measured: an MDN page of 214 KB becomes a few hundred tokens (~1%).
2. **Automatic tiering** — ~85% of sites are served by the lightweight L1 path (fast, highly concurrent). Only blocked pages or JS shells escalate to L2, a real browser with stealth; it never blindly launches browsers per site.
3. **Login walls handled honestly** — returns `login_required` / `blocked` instead of hammering the site, and never lets a captcha page masquerade as article text.

**One-line usage**

```
web_fetch(url="https://example.com/article/1") → JSON: {ok, content, token_estimate, tier, blocked?, login_required?, cached?}
```

**Site types covered**: articles, documentation, portals and listing pages, JS-rendered SPAs, and sites that only reveal content after login.

**Measured on real sites** (reproducible via `scripts/demo.py`):

| Site | Type | Tier | Original | Extracted | Note |
|---|---|---|---|---|---|
| MDN CORS docs | documentation | L1 | 260 KB | ~3.7k tokens (~5.7%) | static, shortest path |
| A news portal home | portal/listing | L2 | 338 KB | ~1.4k tokens (~1.6%) | dynamic page, browser render |
| A tech blog home | SPA | L2 | 12 KB | ~1.4k tokens | JS shell, needs rendering |

**Shape**: a local MCP server (stdio or HTTP) with privacy under your control. Tools: `web_fetch`, `web_batch`, `web_extract`, `web_crawl`, `login`, `session_*`. Caching is on by default.

---

## Features

- **Tiered fetching**: L1 `curl_cffi` → L2 Playwright, escalating only when blocked. Once L2 actually returns content, the host is remembered as "needs a browser" for 10 minutes, so later calls go straight to L2 (`skip_l1:sticky_l2`) instead of paying for a blocked L1; the TTL expiring or a render failure falls back to L1.
- **L1 connection reuse**: one connection pool per fingerprint template inside the process, skipping repeated TLS handshakes and DNS lookups; rebuilt automatically when the event loop changes, released on shutdown or `aclose()`.
- **L2 browser pool**: contexts are reused per host + session (each with its own login state) and evicted least-recently-used when over the cap; concurrent pages are bounded and queue up; an idle browser is closed after `L2_IDLE_TIMEOUT` seconds and relaunched on demand.
- **Content-density root picking**: chooses the tightest container that still holds the whole body, avoiding nav/sidebars.
- **Compact output**: attribute stripping, boilerplate removal, off-site link demotion, tables to CSV (merged cells padded), fenced code blocks with language tags, `figure` images with absolute URLs plus captions. Compression typically 1%–20% (e.g. MDN 214 KB → a few hundred tokens).
- **Honest blocking reports**: challenge pages and empty shells are never treated as content; you get `blocked` / `block_reason`.
- **Stable error codes + diagnostics**: failures carry `error_code` and `retryable`; `debug=true` exposes tier decisions, selector hit counts and L2 interaction results.
- **Persisted logins + named sessions**: `login` opens a visible browser and saves the session; `web_fetch` reuses it. `session="work"` isolates identities, and `session_export` / `session_import` move cookies between machines.
- **Non-HTML content**: `web_fetch` reads plain text directly; with the `[pdf]` extra it extracts PDF text per page, returning `empty` for scanned files without a text layer instead of inventing content.
- **Two cache layers**: HTTP responses are cached on disk with a TTL and revalidated with `ETag` / `Last-Modified`; compacted content and extraction results get their own cache so repeat calls return immediately. `bypass_cache=true` skips both layers. `web_batch` fetches a list concurrently.
- **Lightweight crawling**: `web_crawl` supports link discovery via `depth` / `limit`, or "next page" pagination via `next_selector` (`max_pages` cap, automatic dedup, same-host by default). Static assets are skipped.
- **Total budget with degradation**: `web_fetch` / `web_extract` / `web_batch` / `web_crawl` accept `deadline_ms`. When time runs short it degrades step by step — cap each request → skip retries → skip L2 rendering → return `timeout` — while any content already fetched is still returned. Degraded results are never cached, so later calls with a larger budget cannot inherit truncated content. The budget also covers the compliance stage (rate-limit wait and robots.txt fetch): if it is too tight, the call returns `timeout` early instead of burning the budget on preparation.
- **Structured extraction**: `web_extract` supports `text` / `html` / `attr` / `count` / `list` / `object` / `jsonld`, plus `regex`, `abs_url`, `convert` (int/float/date) and `required`, returning `missing_required` when a mandatory field is absent.
- **Site recipes**: keep per-site rendering hints, root selector, extraction schema and pagination rules in a JSON file; later calls need no arguments, and per-call arguments still win.
- **Selectable output format**: `format=markdown|text|json`; `json` yields a paragraph array ready for storage.
- **Per-host concurrency gate**: at most 2 concurrent requests per host by default, while different hosts still run in parallel.
- **Stealth hardening**: `webdriver` wiped, UA data aligned, deterministic Canvas/WebGL noise, timezone matching.

## Installation

```bash
pip install mcp-scrape                          # core: L1 fetching + compaction + extraction
pip install "mcp-scrape[pdf]"                   # optional: let web_fetch read PDFs
```

```bash
# Browser for L2 rendering (mirror available)
$env:PLAYWRIGHT_DOWNLOAD_HOST="https://cdn.npmmirror.com/binaries/playwright"
pip install "playwright>=1.49"
playwright install chromium

# Or reuse a system browser, no download
$env:SCRAPE_MCP_L2_CHANNEL="chrome"   # or msedge
```

## Running (MCP over stdio)

```bash
scrape-mcp                          # stdio by default; an MCP client takes it over
scrape-mcp --transport streamable-http --port 8000   # plain HTTP
scrape-mcp --version / --help       # version / usage
```

> With no arguments it runs stdio. `--transport` / `--port` are explicit CLI overrides and take precedence over `.env` and environment variables.

## Environment variables (prefix `SCRAPE_MCP_`)

Every setting can be provided as an environment variable, or grouped in a `.env` file at the project root.

```bash
cp .env.example .env    # generate a template
```

Precedence: process environment > `.env` > defaults. `.env` is gitignored; `.env.example` is committed.

| Variable | Default | Description |
|---|---|---|
| `IMPERSONATE` | `chrome` | curl_cffi fingerprint template |
| `REQUEST_TIMEOUT` | `20` | L1 timeout (seconds) |
| `PROXY` | — | Proxy shared by L1 and L2 |
| `L2_ENABLED` | `true` | Enable the Playwright escalation path |
| `L2_CHANNEL` | — | Browser channel: empty = bundled Chromium, `chrome`/`msedge` = system |
| `L2_SETTLE_TIMEOUT` | `8` | Wait window for challenge pages (seconds) |
| `L2_SCROLL_STEPS` | `5` | L2 scroll steps (triggers lazy loading) |
| `L2_SCROLL_DELAY_MS` | `400` | Delay per scroll step (ms) |
| `L2_MAX_PAGES` | `3` | Max pages open at once; extra calls queue |
| `L2_MAX_CONTEXTS` | `8` | Max resident contexts (per host + session); least recently used is evicted |
| `L2_IDLE_TIMEOUT` | `120` | Idle seconds before the browser is closed; `0` keeps it for the process lifetime |
| `L2_TIMEZONE` | `Asia/Shanghai` | Timezone used for stealth alignment |
| `DATA_DIR` | `~/.scrape_mcp` | Where sessions and cache live |
| `RECIPES_DIR` | `<DATA_DIR>/recipes` | Site recipe directory; empty means default |
| `PDF_MAX_PAGES` | `200` | Max pages parsed per PDF (remaining pages are skipped; `pages` still reports the total) |
| `CACHE_TTL` | `300` | TTL for both cache layers (seconds); `0` disables them. HTTP entries are revalidated after expiry; content/extraction hits return directly |
| `RETRY_TIMES` | `2` | Retries for network errors / 5xx (429/403 are not retried) |
| `RETRY_BACKOFF` | `0.5` | Backoff base (seconds, exponential) |
| `RETRY_AFTER_MAX_WAIT` | `0` | Max seconds to wait on `Retry-After` for 429/503; `0` reports only |
| `LOG_LEVEL` | `WARNING` | Log level; JSON lines to stderr (stdout stays for MCP) |
| `BATCH_MAX_CONCURRENCY` | `4` | `web_batch` concurrency cap |
| `BATCH_MAX_URLS` | `20` | `web_batch` batch size cap |
| `HOST_MAX_CONCURRENCY` | `2` | Per-host concurrency cap (`web_batch` / `web_crawl`) |
| `CRAWL_MAX_PAGES` | `20` | Hard cap on pages per `web_crawl` |
| `RESPECT_ROBOTS` | `true` | Follow robots.txt by default; `false` ignores it |
| `ALLOWED_HOSTS` | — | Comma-separated allowlist; when set, only these hosts are allowed |
| `DENIED_HOSTS` | — | Comma-separated denylist, takes precedence over the allowlist |
| `MAX_QPS` | `10` | Global request rate limit (requests/second); `0` disables |
| `HTTP_TOKEN` | — | HTTP access token; when set, all endpoints except `/healthz` require Bearer auth, and it is mandatory for non-loopback binds |
| `HTTP_QPS` | `5` | Per-token request rate (token bucket, allows an equal burst); over-limit returns 429 + `Retry-After`; `0` disables |
| `HTTP_DAILY_QUOTA` | `10000` | Per-token daily request quota (UTC day, resets at midnight); over-limit returns 429; `0` disables |

## HTTP mode (Postman / curl)

Switch to HTTP without code changes:

```bash
$env:SCRAPE_MCP_TRANSPORT="streamable-http"
$env:SCRAPE_MCP_HTTP_PORT="8000"
$env:SCRAPE_MCP_HTTP_TOKEN="<long random string>"   # required for non-loopback binds
python -m scrape_mcp.server
```

It starts with `json_response=1` (the default), so requests and responses are plain JSON rather than streams. **In stateless mode there is no `initialize` handshake — send `tools/call` directly.**

**Access token**: when `SCRAPE_MCP_HTTP_TOKEN` is set, every request except `GET /healthz` must carry:

```bash
curl -H "Authorization: Bearer $env:SCRAPE_MCP_HTTP_TOKEN" http://127.0.0.1:8000/stats
```

Without a token the server only binds to `127.0.0.1` / `localhost` / `::1`; binding to `0.0.0.0` or any non-loopback address exits immediately to avoid unauthenticated exposure. Missing or wrong tokens get a uniform 401 (with `WWW-Authenticate`), and the response contains no internal paths or version details. The token is read from the request header only and is never logged. Once a token is set, loopback requests must present it too, so a local reverse proxy cannot bypass auth by making every request look like `127.0.0.1`.

**Quota**: with a token configured, its requests are also bounded by `HTTP_QPS` (5 req/s by default) and `HTTP_DAILY_QUOTA` (10000 per UTC day by default, reset at midnight). Over-limit requests get 429 with a `Retry-After` header and a `retry_after_s` field (`error` is `rate_limited` or `quota_exceeded`), so clients can back off as suggested. `/healthz` does not consume quota, and the token-less local mode is not quota-limited.

Health check: `GET /healthz` always stays unauthenticated so container probes and gateways can use it.

Stats: `GET /stats` returns in-process counters. Without a token it only answers loopback clients (anything else gets 403); with a token it is protected by Bearer auth and can be read remotely:

```bash
curl http://127.0.0.1:8000/stats
# {"ok":true,"uptime_s":42,"requests":7,"fetches":5,"result_cache_hits":2,
#  "http_cache_hits":1,"cache_hit_rate":0.286,"by_tier":{"l1":4,"l2":1},
#  "by_code":{"ok":5},"by_kind":{"html":5},"tokens_returned":8120,"bytes_in":1048576,
#  "latency_ms":{"window":1024,"samples":5,"p50":120,"p95":820,"p99":820,
#   "buckets":{"lt_100":2,"lt_300":1,"lt_1000":1,"lt_3000":1,"lt_10000":0,"gte_10000":0},
#   "by_tier":{"l1":{"samples":4,"p50":90,"p95":260},"l2":{"samples":1,"p50":820,"p95":820}}}}
```

`latency_ms` covers the last 1024 fetches (percentiles + fixed histogram buckets + per-tier split), excluding result-cache hits. The window is bounded, so memory stays flat in long-running processes.

`by_tier` is the tier that actually ran, `by_code` reuses the tool error codes (`ok` on success), and `cache_hit_rate` is result-cache hits over total requests. Only counters and byte sizes are kept — no URLs, no page content — and everything resets on restart.

Required: headers `MCP-Method`, `Mcp-Name`; the body's `params` must include `_meta` (`protocolVersion` + `clientCapabilities`).

## MCP client setup (Claude / Cursor / VS Code)

Install first (`pip install mcp-scrape`), so the `scrape-mcp` command is available.

**Generate the config with one command (recommended)**

```bash
scrape-mcp init --client cursor --write     # claude / vscode / json
```

- Prints a diff preview by default; `--write` persists it. Other servers in the target file are preserved.
- `--client json` prints the JSON to stdout without writing, for any client or custom path.
- If a `scrape-mcp` entry already exists it is left untouched; pass `--force` to replace it.
- `--scope global|project` chooses global config or project-local (`.cursor/mcp.json`, `.vscode/mcp.json`).
- `--env KEY=VALUE` writes settings such as `SCRAPE_MCP_PROXY` or `SCRAPE_MCP_L2_CHANNEL` into the client config.
- `--command` overrides the launch command; default prefers the installed `scrape-mcp`, else the current interpreter with `-m scrape_mcp.server`.

Generated shape (Claude/Cursor use `mcpServers`; VS Code uses `servers` with `"type": "stdio"`):

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

- To reuse a system browser: set `SCRAPE_MCP_L2_CHANNEL` to `chrome` or `msedge`.
- Restart the client, then call `web_fetch` / `web_extract` / `web_batch` / `web_crawl` from a conversation.
- Inspect tool definitions first: `scrape-mcp schema --tool web_fetch`, or `--output tools.json` for all.

## MCP tools

- **`web_fetch(url, max_tokens=4000, link_policy="internal", format="markdown", ...)`** → status + compact content
  - `link_policy`: `internal` (keep same-site links, demote off-site) / `all` / `none`
  - `format`: `markdown` (default, keeps headings/lists/links) / `text` (plain) / `json` (`content` is a paragraph array)
  - Fetch options: `headers` / `cookies` (custom requests bypass the shared cache), `timeout`, `bypass_cache`
  - Proxy: `proxy` (e.g. `http://127.0.0.1:7890`, `socks5://127.0.0.1:1080`) applies to this call only, and both L1 and any L2 escalation use the same exit; omit it to fall back to `SCRAPE_MCP_PROXY`. Proxied requests bypass the shared cache so results from different exits never mix
  - Total budget: optional `deadline_ms` caps the whole call; degradation markers are listed under "Error codes and diagnostics"
  - Session: `session` (default `default`); use the same name as `login` to attach the stored session
  - Render options (L2 only): `wait_for` (CSS selector), `scroll` (trigger lazy loading), `click` ("load more")
  - Non-HTML: PDFs return structured text (with the `[pdf]` extra; `kind="pdf"` plus `pages` / `pages_parsed`); plain text is returned as-is (`kind="text"`)
  - Diagnostics: `debug=true` adds `diagnostics` (tier decisions, selector hits, L2 interactions), without page content
- **`web_extract(url, schema=None, ...)`** → structured JSON from a field schema
  - `schema` may be omitted when a site recipe exists; otherwise `invalid_argument`
  - Field types: `text` (default) / `html` / `attr` (with `attr`) / `count` / `list` / `object` / `jsonld` (with `path`)
  - Nesting: `list` + `fields` yields object arrays; `object` extracts inside the first match
  - Post-processing: `regex` + `group` (falls back to `default`), `abs_url`, `strip`
  - Conversion: `convert` accepts `int` / `float` / `date` (strips thousands separators and currency symbols; dates accept ISO and forms like `2026年10月20日`)
  - Required fields: `required: true` makes the call `ok=false` with `error_code=missing_required` and lists names in `required_missing`
  - `jsonld` reads `<script type="application/ld+json">` with dotted paths (multiple scripts / `@graph` merged, arrays expanded, numeric segments are indexes)
  - Fetch/render options match `web_fetch` (without `link_policy` / `format`); non-HTML returns `unsupported_content` — use `web_fetch` instead
- **`web_batch(urls, max_tokens=3000, link_policy="internal", format="markdown", ...)`** → concurrent fetch, one result per URL; a single failure does not sink the batch
  - Supports `headers` / `cookies` / `timeout` / `bypass_cache` / `debug` / `session` for the whole batch
  - `proxy` also applies to every URL in the batch; omit it to use `SCRAPE_MCP_PROXY`
  - `deadline_ms` is shared by the batch: tasks that have not started when it runs out return `timeout` without touching the network or browser
  - Reports progress per completed URL when the client requests it (`progressToken`)
- **`web_crawl(url, depth=1, limit=10, same_host=True, max_tokens=2000, ...)`** → crawl page by page; the response's `mode` tells which mode ran
  - `mode=links` (default): discover same-site links by `depth` / `limit`; `depth` max `3`, `limit` bounded by `CRAWL_MAX_PAGES`
  - `mode=paginate`: pass `next_selector` to follow "next page" links; `max_pages` caps it (default `5`), visited URLs are deduped, off-site links are not followed unless `same_host=false`
  - A failing page only affects that page; image/archive links are skipped; `session` applies to every page
  - `proxy` applies to every page of the crawl; omit it to use `SCRAPE_MCP_PROXY`
  - `deadline_ms` budgets the whole crawl: no new pages are started once it runs out, and pages already fetched are returned
  - Reports progress per page when requested (`已翻到第 n/N 页` in paginate mode, `已抓取 n/N 页` in link mode)
- **`login(url, timeout=180, session="default")`** → open a visible browser for a manual login and persist it
  - Use different `session` names for multiple accounts; pass the same name to `web_fetch`
- **`session_list()`** → list stored sessions (`session` + `host` + `updated_at`), without cookie values
- **`session_export(url, session="default", path=None)`** → export cookies as `{host, session, cookies}`; with `path`, also write a file
- **`session_import(url, path, session="default", replace=False)`** → import cookies from a local JSON file
  - Accepts `session_export` output, Playwright `storage_state`, or a bare cookie array
  - Merges by `(name, domain, path)` by default (imported values win); `replace=true` replaces everything. Reads local files only, no network

Tool results are JSON strings in `content[0].text` — parse the text, do not rely on `structured_content`.

Failures carry a stable `error_code` and `retryable`. Transport errors / 5xx are retried with exponential backoff and `attempts` reports the real count. A missed L2 interaction (e.g. a selector that never appears) is only noted in `notes`, not treated as a failure.

With `deadline_ms`, a degraded call shows up in `diagnostics.decisions` as `deadline:skip_retry` (no time left for one more backoff), `deadline:skip_l2` (no time to launch the browser, so the L1 result is returned) or `deadline:exceeded` (the request was never sent).

### Error codes and diagnostics

`error_code` is part of the protocol (additive only), and `retryable` answers "is retrying later worthwhile":

| error_code | retryable | Meaning / next step |
|---|---|---|
| `timeout` / `dns` / `tls` / `connection` / `unknown` | ✅ | Network layer; retry later or check the proxy |
| `http_5xx` | ✅ | Upstream error; retry with backoff |
| `http_4xx` | ❌ | 404/410 etc.; the page itself is invalid |
| `blocked` | ❌ | Anti-bot blocked (Cloudflare/WAF/rate limit); retry later or verify manually |
| `login_required` | ❌ | Login needed; call `login` first |
| `denied` / `robots_disallowed` | ❌ | Rejected by allow/deny lists or robots.txt |
| `missing_required` | ❌ | A required field is missing; see `required_missing` |
| `unsupported_content` | ❌ | Content type unsupported by this tool (e.g. PDF via `web_extract`); use `web_fetch`, install the `[pdf]` extra for PDFs |
| `invalid_url` / `invalid_argument` | ❌ | Bad arguments |

Failures include a compact `diagnostics.decisions` (e.g. `["retry:5xx", "escalate_l2:cloudflare", "l2_ok"]`); a sticky call starts with `skip_l1:sticky_l2`, and a failed render shows `sticky_l2_failed:fallback_l1`. `debug=true` expands counts:

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

For 429/503 with `Retry-After`, the response includes `retry_after_s`; setting `RETRY_AFTER_MAX_WAIT` allows one automatic retry within that bound.

### web_extract example

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

Missing required fields produce `ok=false` + `error_code=missing_required`:

```json
{"ok": false, "error_code": "missing_required", "error": "必填字段缺失: title, price",
 "retryable": false, "required_missing": ["title", "price"], "data": {"title": null}}
```

### Site recipes

Describe how to fetch a site, which node holds the body, which fields to extract, and how to paginate in a JSON file under `RECIPES_DIR` (default `~/.scrape_mcp/recipes`). The filename is the host, or a `host` field overrides it. Matching uses the longest suffix (`m.example.com` hits `example.com`); per-call arguments win over the recipe.

`~/.scrape_mcp/recipes/example.com.json`:

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

| Field | Purpose | Applies to |
|---|---|---|
| `host` | Host to match (defaults to the filename) | all |
| `wait_for` / `scroll` / `click` | L2 render interactions; call arguments win when present | `web_fetch` / `web_extract` / `web_batch` / `web_crawl` |
| `root_selector` | Explicit content root; falls back to density picking if it misses | same |
| `extract` | Default schema when `web_extract` omits `schema` | `web_extract` |
| `crawl.next_selector` / `crawl.max_pages` | Pagination rules; `next_selector` switches to `mode=paginate` | `web_crawl` |
| `note` | Free-form note for humans | — |

No restart is needed: files reload on mtime change. A malformed JSON file is skipped without affecting others.

#### Ready-made recipes (real sites)

`examples/recipes/` contains eight usable recipes — copy them into `RECIPES_DIR`:

```bash
mkdir -p ~/.scrape_mcp/recipes
cp examples/recipes/*.json ~/.scrape_mcp/recipes/
```

| File | Site | Recipe capabilities demonstrated |
|---|---|---|
| `docs.python.org.json` | Python docs | `root_selector` = `div.body`; `crawl.next_selector` via `accesskey="N"`; `extract` for title/version/section count/next link |
| `github.com.json` | GitHub Trending | `type=list` for the whole board (repo, URL, stars today, language, description) |
| `ruanyifeng.com.json` | Ruan Yifeng's blog | `root_selector` = `#main-content` plus `regex` for the publish date; `host` = `ruanyifeng.com` to cover `www.` |
| `developer.mozilla.org.json` | MDN docs | `root_selector` = `main#content` (the site has no `article` element); `extract` for title/section count/body link count |
| `fastapi.tiangolo.com.json` | FastAPI docs | Material for MkDocs site: `root_selector` = `.md-content__inner`, skipping `.md-sidebar` navigation |
| `cn.vuejs.org.json` | Vue docs (zh-CN) | VitePress site: `root_selector` = `.vt-doc` (the older theme used `.vp-doc`) |
| `cnblogs.com.json` | Cnblogs home | List page: one `type=list` extraction yields 20 posts (title/URL/author/summary/time); pagination is numbered links, so no `crawl` |
| `chinanews.com.cn.json` | China News article page | `root_selector` = `.left_zw` with a `required` title; time/source/author live in the BaiduSpider block |

```bash
# Extract via recipe (no schema)
web_extract(url="https://docs.python.org/3/library/asyncio.html")
web_extract(url="https://github.com/trending")
web_extract(url="https://www.ruanyifeng.com/blog/2026/10/weekly-issue-415.html")
web_extract(url="https://developer.mozilla.org/zh-CN/docs/Web/HTTP/Guides/CORS")
web_extract(url="https://fastapi.tiangolo.com/tutorial/")
web_extract(url="https://cn.vuejs.org/guide/introduction.html")
web_extract(url="https://www.cnblogs.com/")
web_extract(url="https://www.chinanews.com.cn/gn/2026/10-21/10718387.shtml")

# Paginate via recipe (next_selector switches to mode=paginate)
web_crawl(url="https://docs.python.org/3/library/asyncio.html", max_pages=3)
```

> Selectors were verified in 2026-10. If a site is redesigned, updating one selector restores it.

Per-call overrides: explicit arguments beat the recipe, e.g. `web_fetch(url="...", wait_for=".comments")`.

## Python SDK

Use the same implementation without an MCP client (identical arguments, payloads and error codes):

```python
import asyncio

from scrape_mcp.sdk import aclose, batch, crawl, extract, fetch


async def main() -> None:
    page = await fetch("https://docs.python.org/3/library/asyncio.html", max_tokens=2000)
    print(page["ok"], page["token_estimate"], page["tier"])

    # Field extraction; without a schema the site recipe is used
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

    await aclose()  # release the browser if L2 ran; optional, avoids shutdown warnings


asyncio.run(main())
```

- Return values are parsed dicts of the MCP payloads; failures still carry `error_code` / `retryable`.
- The SDK lives at `scrape_mcp.sdk` to avoid a name clash with the internal `scrape_mcp.extract` package.

## Example scripts

`examples/` ships five runnable scenario scripts (defaults work out of the box; set `SCRAPE_MCP_PROXY` if you need a proxy):

| Script | Scenario | Common flags |
| --- | --- | --- |
| [01_fetch_article.py](examples/01_fetch_article.py) | Compress an article into a token budget | `--max-tokens` `--out` |
| [02_extract_list.py](examples/02_extract_list.py) | Extract structured data from listing pages | `--json` `--csv` |
| [03_crawl_docs.py](examples/03_crawl_docs.py) | Follow "next page" and merge into one markdown file | `--selector` `--max-pages` `--out` |
| [04_pdf_to_text.py](examples/04_pdf_to_text.py) | Read a PDF as text (needs `mcp-scrape[pdf]`) | `--out` `--max-tokens` |
| [05_batch_check.py](examples/05_batch_check.py) | Batch health check; prints tier/status/tokens/error code, non-zero exit on failure | URLs as positional args |

```bash
python examples/01_fetch_article.py https://ruanyifeng.com/blog/ --max-tokens 1200
python examples/02_extract_list.py --csv trending.csv
python examples/03_crawl_docs.py --max-pages 5 --out docs.md
```

Also see `examples/example_client.py` (MCP client call example) and `examples/recipes/*.json`.

## Benchmarks

Two benchmarks with different purposes: `bench.py` measures the latency win from caching, `bench_sites.py` measures whether real sites return the right, complete content.

### Cache and latency

```bash
python scripts/bench.py                              # cold vs. warm
python scripts/bench.py --l2 https://news.qq.com/    # also measure browser cost
```

"Cold" means `bypass_cache=true` (network + compaction); "warm" is a normal call that hits the content cache.
Measured in 2026-10 on Windows 11 over a home connection (`rounds=5`, `max_tokens=4000`):

| Page | Tier | Cold p50 | Warm p50 | Speedup | Tokens |
| --- | --- | --- | --- | --- | --- |
| example.com | L2 | 495 ms | 1.4 ms | ~360× | 330 |
| docs.python.org (asyncio-task) | L1 | 681 ms | 1.0 ms | ~650× | 3588 |

L2 browser: first call in a process ≈3.5 s (including launch), reused p50 ≈1.8 s.
Absolute numbers depend heavily on network and target site; the cold-vs-warm ratio on one machine is what matters.

### Real-site quality

```bash
python scripts/bench_sites.py                          # all sites
python scripts/bench_sites.py --nightly                # nightly subset
python scripts/bench_sites.py --category docs,list     # filter by category
python scripts/bench_sites.py --json out.json --md out.md
python scripts/bench_sites.py --baseline out.json      # compare with the previous run
python scripts/bench_sites.py --nightly --baseline benchmarks/baseline.json  # compare with the repo baseline
pip install trafilatura                                # optional: compare with a text-only extractor
python scripts/bench_sites.py --reference trafilatura
```

The corpus lives in [benchmarks/sites.json](benchmarks/sites.json) and is grouped into docs / blog / news / list / SPA / login-wall and anti-bot / PDF. Each site can define:

- `expect`: `ok` means content should be fetched; an error code such as `login_required` or `robots_disallowed` means the site should be reported as blocked, not faked as success
- `min_chars`: lower bound for content characters
- `checks`: stable snippets that must appear in the content (only configured for structurally stable pages; dynamic homepages only check `min_chars`)

The output covers pass rate, fetch rate, interception rate, completeness, tokens, p50/p95 latency and tier distribution, and can be saved as JSON / Markdown.
`--baseline` compares per site with a previous run and flags a regression when content characters drop by more than 20%.

The repo keeps a nightly-subset baseline in [benchmarks/baseline.json](benchmarks/baseline.json), first recorded on 2026-10-21 (10/10 passed, p50 453 ms / p95 2813 ms). It only covers the nightly subset and is meant for trend checking, not as the pass rate of the full corpus. Update it monthly: download a `bench-sites.json`, set `sites_file` to a relative path, add a `source` field, and replace the file.

**Local run (2026-10-21, 24 sites)**: 18/24 passed, completeness 17/19, interception 3/3, latency p50 2809 ms / p95 9294 ms.
Of the 6 failures, 5 were unreachable from the local network (MDN / GitHub / Hacker News / V2EX / arXiv timed out) and 1 was blocked by the site (Stack Overflow returned `blocked`).
The nightly job reruns these sites on GitHub's network; its `site-quality-<run_id>` artifact is the durable record.
Nightly baseline (2026-10-21, after fixing the PDF dependency): 10/10 passed, 9/9 content fetched, 10/10 completeness, p50 453 ms / p95 2813 ms.

`--reference trafilatura` only counts plain body text; this tool keeps headings, lists and links, so token counts are not directly equivalent — treat them as an order-of-magnitude check.

## Docker

```bash
# L1 only (smaller image, covers most pages)
docker build -t scrape-mcp .

# With L2 real browser (Cloudflare/captcha/SPA)
docker build --build-arg INSTALL_BROWSER=1 -t scrape-mcp:l2 .

docker run -p 8002:8002 -e SCRAPE_MCP_HTTP_TOKEN=<token> scrape-mcp
curl http://127.0.0.1:8002/healthz     # {"ok":true,"version":"…"}
```

The container binds `0.0.0.0`, so the token must be supplied with `-e SCRAPE_MCP_HTTP_TOKEN=...`; `/healthz` stays unauthenticated for the HEALTHCHECK.

## Project layout

```
benchmarks/
├── sites.json         # Real-site quality corpus (tiers, expected outcome, completeness checks)
└── baseline.json      # Nightly-subset baseline for trend checks (updated monthly)
src/scrape_mcp
├── server.py          # MCP entry + tools (web_fetch/web_extract/web_batch/web_crawl/login/session_*)
├── http_auth.py       # HTTP Bearer middleware + non-loopback bind guard
├── cli.py             # Subcommands: init (client config) / schema (export tool definitions)
├── sdk.py             # Python SDK wrappers (fetch/extract/crawl/batch/aclose)
├── config.py          # Environment configuration
├── tokenizer.py       # tiktoken counting + budget truncation
├── core/
│   ├── http_client.py # L1 curl_cffi + disk cache
│   ├── fetcher.py     # Tiered dispatch (L1→L2), returns FetchOutcome
│   ├── detector.py    # Block / shell page detection
│   ├── browser.py     # L2 Playwright + stealth
│   ├── session.py     # Session persistence (named sessions + cookie import/export)
│   ├── recipes.py     # Site recipes (longest-suffix match, mtime reload)
│   ├── cache.py       # HTTP response cache + content/extraction result cache
│   ├── stats.py       # In-process counters (source for /stats)
│   ├── errors.py      # Stable error codes
│   └── hostlimit.py   # Per-host concurrency gate
└── extract/
    ├── compact.py     # DOM pruning → token-efficient content
    ├── schema.py      # Structured extraction from a field schema
    ├── links.py       # Link discovery (web_crawl)
    ├── pdf.py         # PDF → text (optional pypdf)
    └── render.py      # markdown → plain text / blocks
```

## Development

```bash
pip install -e ".[dev]"
python -m pytest
```

## License

MIT — see [LICENSE](LICENSE).
