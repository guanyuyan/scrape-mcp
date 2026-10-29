"""内置看板：`GET /` 返回的单文件 HTML，只读 `/stats` 与 `/metrics`，不含任何服务端状态。

刻意不引入前端框架与外部资源：页面内联 CSS/JS，离线可用，也不往 MCP 依赖里加东西。
数据仍受既有鉴权保护——页面本身是空壳，令牌由用户填一次并存进 localStorage。
"""

from __future__ import annotations

from . import __version__

# 刷新间隔（毫秒）：与 /stats 的统计窗口（最近 1024 次）相比足够细，又不至于刷屏
REFRESH_MS = 5000

HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>scrape-mcp 运行看板</title>
<style>
:root {
  --bg: #0f1419; --panel: #171d26; --line: #232c38; --fg: #e6edf3; --dim: #8b98a9;
  --accent: #4aa8ff; --ok: #3fb950; --warn: #d29922; --bad: #f85149;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 24px; background: var(--bg); color: var(--fg);
  font: 14px/1.5 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
}
header { display: flex; flex-wrap: wrap; gap: 12px; align-items: baseline; margin-bottom: 18px; }
h1 { font-size: 20px; margin: 0; font-weight: 600; }
.meta { color: var(--dim); font-size: 13px; }
.spacer { flex: 1; }
.hidden { display: none !important; }
.grid { display: grid; gap: 14px; grid-template-columns: 1fr; }
@media (min-width: 700px) { .grid { grid-template-columns: repeat(2, 1fr); } }
@media (min-width: 1100px) { .grid { grid-template-columns: repeat(3, 1fr); } }
.card {
  background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  padding: 14px 16px; min-height: 110px;
}
.card h2 { font-size: 13px; margin: 0 0 10px; color: var(--dim); font-weight: 500; letter-spacing: .02em; }
.big { font-size: 26px; font-weight: 600; font-variant-numeric: tabular-nums; }
.kv { display: flex; flex-wrap: wrap; gap: 10px 22px; }
.kv div { min-width: 88px; }
.kv span { display: block; color: var(--dim); font-size: 12px; }
.kv b { font-weight: 600; font-variant-numeric: tabular-nums; }
.bar { height: 6px; border-radius: 3px; background: var(--line); overflow: hidden; margin-top: 6px; }
.bar i { display: block; height: 100%; background: var(--accent); border-radius: 3px; }
.row { display: flex; justify-content: space-between; gap: 12px; margin-top: 8px; font-size: 13px; }
.row .name { color: var(--dim); }
.row .val { font-variant-numeric: tabular-nums; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip {
  padding: 2px 8px; border-radius: 999px; background: var(--line); font-size: 12px;
  color: var(--fg); font-variant-numeric: tabular-nums;
}
#tokenBar { display: flex; gap: 8px; align-items: center; margin-bottom: 14px; }
input, button {
  font: inherit; color: var(--fg); background: #10161e; border: 1px solid var(--line);
  border-radius: 8px; padding: 6px 10px;
}
button { cursor: pointer; }
button:hover { border-color: var(--accent); }
#err { color: var(--bad); font-size: 13px; }
</style>
</head>
<body>
<header>
  <h1>scrape-mcp 运行看板</h1>
  <span class="meta">v__VERSION__ · 每 __REFRESH_S__ 秒刷新</span>
  <span class="spacer"></span>
  <span class="meta" id="clock"></span>
  <button id="logout" class="hidden">清除令牌</button>
</header>

<div id="tokenBar" class="hidden">
  <input id="token" type="password" placeholder="HTTP 访问令牌（Bearer）" size="34">
  <button id="save">保存</button>
  <span class="meta">仅存本机 localStorage；未配置令牌的服务无需填写</span>
</div>

<div class="grid">
  <section class="card">
    <h2>请求</h2>
    <div class="big" id="requests">–</div>
    <div class="kv" style="margin-top:10px">
      <div><span>实际抓取</span><b id="fetches">–</b></div>
      <div><span>结果缓存命中</span><b id="resultHits">–</b></div>
      <div><span>HTTP 缓存命中</span><b id="httpHits">–</b></div>
    </div>
  </section>

  <section class="card">
    <h2>分层分布</h2>
    <div id="tiers"></div>
  </section>

  <section class="card">
    <h2>抓取耗时（最近窗口）</h2>
    <div class="kv">
      <div><span>p50</span><b id="p50">–</b></div>
      <div><span>p95</span><b id="p95">–</b></div>
      <div><span>p99</span><b id="p99">–</b></div>
    </div>
    <div id="buckets" style="margin-top:10px"></div>
  </section>

  <section class="card">
    <h2>结果码分布</h2>
    <div id="codes"></div>
  </section>

  <section class="card">
    <h2>正文类型与体量</h2>
    <div class="chips" id="kinds"></div>
    <div class="kv" style="margin-top:12px">
      <div><span>返回 token</span><b id="tokens">–</b></div>
      <div><span>入站字节</span><b id="bytes">–</b></div>
    </div>
  </section>

  <section class="card">
    <h2>资源占用（来自 /metrics）</h2>
    <div class="kv">
      <div><span>L2 在途页</span><b id="l2Active">–</b></div>
      <div><span>L2 排队</span><b id="l2Queued">–</b></div>
      <div><span>L2 上下文</span><b id="l2Ctx">–</b></div>
      <div><span>代理池健康/总数</span><b id="pool">–</b></div>
    </div>
  </section>
</div>

<div id="err" style="margin-top:12px"></div>

<script>
const REFRESH_MS = __REFRESH_MS__;
const $ = (id) => document.getElementById(id);
let token = localStorage.getItem("scrape_mcp_token") || "";

function fmt(n) {
  if (n === undefined || n === null) return "–";
  if (n >= 1e9) return (n / 1e9).toFixed(1) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}

async function get(url, asText) {
  const headers = token ? { Authorization: "Bearer " + token } : {};
  const resp = await fetch(url, { headers });
  if (resp.status === 401) { showTokenBar(true); throw new Error("未授权：请填写访问令牌"); }
  if (!resp.ok) throw new Error(url + " → " + resp.status);
  return asText ? resp.text() : resp.json();
}

function showTokenBar(show) {
  $("tokenBar").classList.toggle("hidden", !show);
  $("logout").classList.toggle("hidden", !token);
}

function rows(el, data, total, unit) {
  const entries = Object.entries(data || {}).sort((a, b) => b[1] - a[1]);
  el.innerHTML = "";
  for (const [name, value] of entries) {
    const pct = total ? Math.round((value / total) * 100) : 0;
    const row = document.createElement("div");
    row.innerHTML =
      '<div class="row"><span class="name"></span><span class="val"></span></div>' +
      '<div class="bar"><i style="width:' + pct + '%"></i></div>';
    row.querySelector(".name").textContent = name;
    row.querySelector(".val").textContent = fmt(value) + (unit || "") + " · " + pct + "%";
    el.appendChild(row);
  }
  if (!entries.length) el.innerHTML = '<div class="meta">暂无数据</div>';
}

function parseMetrics(text) {
  const out = {};
  for (const line of text.split("\\n")) {
    if (!line || line.startsWith("#")) continue;
    const m = line.match(/^([a-zA-Z0-9_]+)(\\{[^}]*\\})?\\s+([0-9.eE+-]+)$/);
    if (m) out[m[1]] = Number(m[3]);
  }
  return out;
}

async function refresh() {
  try {
    const stats = await get("/stats");
    const total = stats.fetches || 0;
    $("requests").textContent = fmt(stats.requests);
    $("fetches").textContent = fmt(stats.fetches);
    $("resultHits").textContent = fmt(stats.result_cache_hits) + " · " + Math.round((stats.cache_hit_rate || 0) * 100) + "%";
    $("httpHits").textContent = fmt(stats.http_cache_hits);
    $("tokens").textContent = fmt(stats.tokens_returned);
    $("bytes").textContent = fmt(stats.bytes_in);
    rows($("tiers"), stats.by_tier, total);
    rows($("codes"), stats.by_code, total);

    const kinds = stats.by_kind || {};
    $("kinds").innerHTML = Object.entries(kinds)
      .map(([k, v]) => '<span class="chip">' + k + " " + fmt(v) + "</span>").join("") ||
      '<span class="meta">暂无数据</span>';

    const lat = stats.latency_ms || {};
    $("p50").textContent = lat.p50 + " ms";
    $("p95").textContent = lat.p95 + " ms";
    $("p99").textContent = lat.p99 + " ms";
    const labels = { lt_100: "<100ms", lt_300: "100–300", lt_1000: "300–1s", lt_3000: "1–3s", lt_10000: "3–10s", gte_10000: "≥10s" };
    rows($("buckets"), Object.fromEntries(Object.entries(lat.buckets || {}).map(([k, v]) => [labels[k] || k, v])), lat.samples || 0);

    try {
      const m = parseMetrics(await get("/metrics", true));
      $("l2Active").textContent = fmt(m["scrape_mcp_l2_active_pages"]);
      $("l2Queued").textContent = fmt(m["scrape_mcp_l2_queued_pages"]);
      $("l2Ctx").textContent = fmt(m["scrape_mcp_l2_contexts"]);
      $("pool").textContent = m["scrape_mcp_proxy_pool_size"] === undefined
        ? "未配置"
        : fmt(m["scrape_mcp_proxy_pool_healthy"]) + " / " + fmt(m["scrape_mcp_proxy_pool_size"]);
    } catch (e) { /* /metrics 被策略拒绝时不影响主视图 */ }

    $("clock").textContent = "已运行 " + Math.floor((stats.uptime_s || 0) / 60) + " 分钟 · " +
      new Date().toLocaleTimeString();
    $("err").textContent = "";
  } catch (e) {
    $("err").textContent = String(e.message || e);
  }
}

$("save").onclick = () => {
  token = $("token").value.trim();
  localStorage.setItem("scrape_mcp_token", token);
  showTokenBar(false);
  refresh();
};
$("logout").onclick = () => {
  token = "";
  localStorage.removeItem("scrape_mcp_token");
  showTokenBar(true);
};

// 先按"未配置令牌"显示；真遇到 401 再把令牌栏亮出来，避免无鉴权部署时多一个空输入框
showTokenBar(false);
refresh();
setInterval(() => { if (!document.hidden) refresh(); }, REFRESH_MS);
</script>
</body>
</html>
"""


def render() -> str:
    """把版本号与刷新间隔嵌进静态页；其余内容与进程状态无关。"""
    return (
        HTML.replace("__VERSION__", __version__)
        .replace("__REFRESH_S__", str(REFRESH_MS // 1000))
        .replace("__REFRESH_MS__", str(REFRESH_MS))
    )
