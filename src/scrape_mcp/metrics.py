"""Prometheus 文本格式指标（`/metrics`）：只暴露计数与占用，不含 URL 与页面内容。

不引入 prometheus_client：这里需要的只是计数器与几个 gauge，直接按文本格式
（version=0.0.4）拼装，少一个依赖。抓取计数取自 core.stats（与 `/stats` 同一口径），
鉴权/限流拒绝与 L2 占用由本模块和 BrowserRenderer 补充。
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable

from . import __version__
from .core.stats import get_stats

# Prometheus 文本格式的 content type（带编码，Starlette 不会再追加 charset）
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# HTTP 层拒绝原因：unauthorized（无/错令牌）、forbidden（无令牌时的非回环来源）、
# tool_forbidden（工具不在租户白名单）、rate_limited（QPS 超限）、quota_exceeded（日配额耗尽）
_REJECTION_REASONS = (
    "unauthorized",
    "forbidden",
    "tool_forbidden",
    "rate_limited",
    "quota_exceeded",
)
_rejections: Counter[str] = Counter()

# 多令牌时按租户统计：只记计入配额的已通过请求，以及带租户归属的 429 / 403
_tenant_requests: Counter[str] = Counter()
_tenant_rejections: Counter[tuple[str, str]] = Counter()
# 通过权限与配额的 tools/call 按 (租户, 工具) 计数，用于按租户看工具分布
_tenant_tool_calls: Counter[tuple[str, str]] = Counter()

# 抓取耗时直方图（秒）：桶边界与 `/stats` 的毫秒分桶一致，累计值在渲染时求和
_LATENCY_BUCKETS: tuple[float, ...] = (0.1, 0.3, 1.0, 3.0, 10.0)
_latency_buckets: Counter[tuple[str, int]] = Counter()
_latency_sum_ms: Counter[str] = Counter()
_latency_count: Counter[str] = Counter()

# 租户配额余量由中间件注入（它持有各租户的限流器），这里只负责渲染
_tenant_quota_provider: Callable[[], dict[str, int]] | None = None


def observe_latency(tier: str, ms: int) -> None:
    """记一次抓取耗时；按层级落进直方图桶，未落入任何桶的归到 ``+Inf``。"""
    name = tier or "unknown"
    elapsed_ms = max(0, int(ms))
    secs = elapsed_ms / 1000.0
    idx = next(
        (i for i, bound in enumerate(_LATENCY_BUCKETS) if secs < bound), len(_LATENCY_BUCKETS)
    )
    _latency_buckets[(name, idx)] += 1
    _latency_sum_ms[name] += elapsed_ms
    _latency_count[name] += 1


def set_tenant_quota_provider(provider: Callable[[], dict[str, int]] | None) -> None:
    """注册租户配额余量的读取函数；传 None 表示不再输出该指标。"""
    global _tenant_quota_provider
    _tenant_quota_provider = provider


def record_rejection(reason: str) -> None:
    """记一次 HTTP 拒绝；由 BearerAuthMiddleware 在返回 401/403/429 时调用。"""
    _rejections[reason or "unknown"] += 1


def record_tenant_request(tenant: str) -> None:
    """记一次通过鉴权且计入配额的请求（/healthz 与 /metrics 不计）。"""
    _tenant_requests[tenant] += 1


def record_tenant_rejection(tenant: str, reason: str) -> None:
    """记一次带租户归属的拒绝（429 配额类与 403 工具白名单）。"""
    _tenant_rejections[(tenant, reason or "unknown")] += 1


def record_tenant_tool(tenant: str, tool: str) -> None:
    """记一次已通过权限与配额的 tools/call，用于按租户统计工具用量。"""
    _tenant_tool_calls[(tenant, tool or "unknown")] += 1


def tenant_snapshot() -> dict[str, dict[str, object]]:
    """按租户汇总的用量：请求数、工具分布、拒绝原因与当日剩余配额（`/stats` 用）。"""
    names = (
        set(_tenant_requests)
        | {tenant for tenant, _ in _tenant_rejections}
        | {tenant for tenant, _ in _tenant_tool_calls}
    )
    quotas = _tenant_quota_provider() if _tenant_quota_provider is not None else {}
    snapshot: dict[str, dict[str, object]] = {}
    for name in sorted(names):
        tools = {
            tool: count for (tenant, tool), count in _tenant_tool_calls.items() if tenant == name
        }
        reasons = {
            reason: count
            for (tenant, reason), count in _tenant_rejections.items()
            if tenant == name
        }
        item: dict[str, object] = {
            "requests": int(_tenant_requests[name]),
            "tool_calls": dict(sorted(tools.items())),
            "rejections": dict(sorted(reasons.items())),
        }
        if name in quotas:
            item["quota_remaining"] = int(quotas[name])
        snapshot[name] = item
    return snapshot


def reset() -> None:
    """清空本模块自有的计数；测试用，避免用例之间互相累加。"""
    global _tenant_quota_provider
    _rejections.clear()
    _tenant_requests.clear()
    _tenant_rejections.clear()
    _tenant_tool_calls.clear()
    _latency_buckets.clear()
    _latency_sum_ms.clear()
    _latency_count.clear()
    _tenant_quota_provider = None


def _escape(value: str) -> str:
    """label 值转义：反斜杠、换行与双引号，避免把文本格式拼坏。"""
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _plain(lines: list[str], name: str, kind: str, help_text: str, value: int) -> None:
    lines.extend(
        [
            f"# HELP {name} {help_text}",
            f"# TYPE {name} {kind}",
            f"{name} {value}",
        ]
    )


def _labelled(
    lines: list[str],
    name: str,
    kind: str,
    help_text: str,
    values: Counter[str] | dict[str, int],
    label: str,
) -> None:
    lines.extend([f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"])
    for key in sorted(values):
        lines.append(f'{name}{{{label}="{_escape(str(key))}"}} {int(values[key])}')


def render(l2: dict[str, int] | None = None, proxy: dict[str, int] | None = None) -> str:
    """渲染当前进程的指标文本；l2 为空表示浏览器从未启动，proxy 为空表示未配置代理池。"""
    stats = get_stats()
    requests = stats.fetches + stats.result_cache_hits
    snap = l2 or {}

    lines: list[str] = []
    lines.extend(
        [
            "# HELP scrape_mcp_build_info 版本信息（恒为 1，版本变化即重新部署）",
            "# TYPE scrape_mcp_build_info gauge",
            f'scrape_mcp_build_info{{version="{_escape(__version__)}"}} 1',
        ]
    )
    _plain(
        lines,
        "scrape_mcp_uptime_seconds",
        "gauge",
        "进程已运行的秒数",
        int(time.time() - stats.started_at),
    )
    _plain(
        lines,
        "scrape_mcp_requests_total",
        "counter",
        "请求总数（进入抓取链路 + 结果缓存命中）",
        requests,
    )
    _plain(
        lines,
        "scrape_mcp_fetches_total",
        "counter",
        "进入抓取链路的次数（含 HTTP 缓存命中，不含结果缓存命中）",
        stats.fetches,
    )
    _plain(
        lines,
        "scrape_mcp_result_cache_hits_total",
        "counter",
        "结果缓存命中次数（不产生网络与解析）",
        stats.result_cache_hits,
    )
    _plain(
        lines,
        "scrape_mcp_http_cache_hits_total",
        "counter",
        "HTTP 响应缓存命中次数（含 304 复用）",
        stats.http_cache_hits,
    )
    _plain(
        lines,
        "scrape_mcp_tokens_returned_total",
        "counter",
        "返回给调用方的正文 token 累计",
        stats.tokens_returned,
    )
    _plain(
        lines,
        "scrape_mcp_bytes_in_total",
        "counter",
        "入站 HTML 字节累计",
        stats.bytes_in,
    )
    _labelled(
        lines,
        "scrape_mcp_fetch_tier_total",
        "counter",
        "按实际生效层级统计的抓取次数（l1=curl_cffi，l2=Playwright）",
        {"l1": 0, "l2": 0} | dict(stats.by_tier),
        "tier",
    )
    _labelled(
        lines,
        "scrape_mcp_fetch_code_total",
        "counter",
        "按错误码统计的抓取次数（成功为 ok）",
        stats.by_code,
        "code",
    )
    _labelled(
        lines,
        "scrape_mcp_fetch_kind_total",
        "counter",
        "按内容形态统计的抓取次数（html / pdf / text）",
        stats.by_kind,
        "kind",
    )
    lines.extend(
        [
            "# HELP scrape_mcp_fetch_duration_seconds 抓取耗时分布（按实际层级，单位秒）",
            "# TYPE scrape_mcp_fetch_duration_seconds histogram",
        ]
    )
    # 桶值需累计：Prometheus histogram 的 le 是“小于等于”，这里把离散桶累加后再输出
    for tier in sorted(_latency_count.keys() | {"l1", "l2"}):
        label = _escape(tier)
        cumulative = 0
        for idx, bound in enumerate(_LATENCY_BUCKETS):
            cumulative += _latency_buckets[(tier, idx)]
            lines.append(
                f'scrape_mcp_fetch_duration_seconds_bucket{{tier="{label}",'
                f'le="{bound:g}"}} {cumulative}'
            )
        total = _latency_count[tier]
        lines.append(
            f'scrape_mcp_fetch_duration_seconds_bucket{{tier="{label}",le="+Inf"}} {total}'
        )
        lines.append(
            f'scrape_mcp_fetch_duration_seconds_sum{{tier="{label}"}} '
            f"{_latency_sum_ms[tier] / 1000:.3f}"
        )
        lines.append(f'scrape_mcp_fetch_duration_seconds_count{{tier="{label}"}} {total}')
    _labelled(
        lines,
        "scrape_mcp_http_rejections_total",
        "counter",
        "鉴权与限流拒绝次数（unauthorized / forbidden / tool_forbidden / rate_limited /"
        " quota_exceeded）",
        dict.fromkeys(_REJECTION_REASONS, 0) | dict(_rejections),
        "reason",
    )
    _labelled(
        lines,
        "scrape_mcp_tenant_requests_total",
        "counter",
        "按租户统计的已通过请求数（只含计入配额的请求，/healthz 与 /metrics 不计）",
        _tenant_requests,
        "tenant",
    )
    lines.extend(
        [
            "# HELP scrape_mcp_tenant_rejections_total 按租户统计的拒绝次数"
            "（429 限流/配额与 403 工具白名单）",
            "# TYPE scrape_mcp_tenant_rejections_total counter",
        ]
    )
    for (tenant, reason), value in sorted(_tenant_rejections.items()):
        lines.append(
            f'scrape_mcp_tenant_rejections_total{{tenant="{_escape(tenant)}",'
            f'reason="{_escape(reason)}"}} {value}'
        )
    lines.extend(
        [
            "# HELP scrape_mcp_tenant_tool_calls_total 按租户与工具统计的已执行调用次数",
            "# TYPE scrape_mcp_tenant_tool_calls_total counter",
        ]
    )
    for (tenant, tool), value in sorted(_tenant_tool_calls.items()):
        lines.append(
            f'scrape_mcp_tenant_tool_calls_total{{tenant="{_escape(tenant)}",'
            f'tool="{_escape(tool)}"}} {value}'
        )

    quotas = _tenant_quota_provider() if _tenant_quota_provider is not None else {}
    if quotas:
        _labelled(
            lines,
            "scrape_mcp_tenant_quota_remaining",
            "gauge",
            "租户当日剩余请求配额（只输出设置了 daily_quota 的租户）",
            quotas,
            "tenant",
        )

    _plain(
        lines,
        "scrape_mcp_l2_browser_running",
        "gauge",
        "L2 浏览器进程是否已启动（1=运行中，0=未启动或已空闲回收）",
        int(snap.get("running", 0)),
    )
    _plain(
        lines,
        "scrape_mcp_l2_active_pages",
        "gauge",
        "当前在途的 L2 渲染页数（不含排队等待的请求）",
        int(snap.get("active_pages", 0)),
    )
    _plain(
        lines,
        "scrape_mcp_l2_queued_pages",
        "gauge",
        "排队等待 L2 渲染页位的请求数",
        int(snap.get("queued", 0)),
    )
    _plain(
        lines,
        "scrape_mcp_l2_contexts",
        "gauge",
        "常驻 L2 浏览器上下文数（按 host + 会话 + 代理复用）",
        int(snap.get("contexts", 0)),
    )
    _plain(
        lines,
        "scrape_mcp_l2_max_pages",
        "gauge",
        "L2 并发渲染页上限（配置 L2_MAX_PAGES）",
        int(snap.get("max_pages", 0)),
    )
    _plain(
        lines,
        "scrape_mcp_l2_max_contexts",
        "gauge",
        "常驻 L2 上下文上限（配置 L2_MAX_CONTEXTS）",
        int(snap.get("max_contexts", 0)),
    )

    pool = proxy or {}
    if pool:
        _plain(
            lines,
            "scrape_mcp_proxy_pool_size",
            "gauge",
            "代理池出口总数（未配置代理池时无此指标）",
            int(pool.get("size", 0)),
        )
        _plain(
            lines,
            "scrape_mcp_proxy_pool_healthy",
            "gauge",
            "代理池中未处于冷却期的出口数",
            int(pool.get("healthy", 0)),
        )
        _plain(
            lines,
            "scrape_mcp_proxy_pool_cooling",
            "gauge",
            "代理池中处于失败冷却期的出口数",
            int(pool.get("cooling", 0)),
        )
    return "\n".join(lines) + "\n"
