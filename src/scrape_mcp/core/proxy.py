"""代理：URL 校验、池化调度（轮询、按站点粘性、失败冷却）、会话绑定与地区标签。

一次抓取只在入口选一次出口，L1、重试与 L2 升级共用同一个代理，
避免同一请求在多个 IP 之间跳来跳去（登录态与风控都会因此失效）。

出口优先级：单请求 proxy > 会话绑定代理 > 地区匹配的池出口 > 全局 proxy。
代理池条目可带地区标签与类别（``http://host:port|us|residential``），
``dc`` 是数据中心出口（默认），``residential`` 用于被限流后升级重试；
会话绑定用 ``SCRAPE_MCP_PROXY_SESSIONS=work=http://...`` 配置，让同一会话
（如某个站点账号）始终走同一个 IP。

住宅代理还可通过服务商模板接入（``build_provider_proxy``）：
模板形如 ``http://{user}:{pass}@gate.provider.com:8000``，按站点派生稳定
session 或每请求轮换，由 ``SCRAPE_MCP_PROXY_PROVIDER_*`` 配置驱动。
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

# curl_cffi 与 Playwright 都支持这几类代理协议
PROXY_SCHEMES = frozenset({"http", "https", "socks4", "socks5"})

# 出口类别：dc=数据中心（默认），residential=住宅。只有住宅会用于"被拦后升级"
PROXY_KINDS = ("dc", "residential")
_KIND_DEFAULT = "dc"

# 代理列表分隔符：逗号、分号、空白都接受，方便从 .env 里一行写多个
_SPLIT = re.compile(r"[,;\s]+")
# 代理条目内的地区/类别分隔符：URL 里不会出现 ``|``，不会与账号密码混淆
_REGION_SEP = "|"
# 会话名与地区标签都以字母数字为主，避免用户把控制字符/路径分隔符写进配置
_SESSION_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_REGION_NAME = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def valid_proxy(raw: object) -> str | None:
    """校验代理 URL 并返回去掉首尾空白的原文；非法返回 None。"""
    text = raw.strip() if isinstance(raw, str) else ""
    if not text:
        return None
    parsed = urlparse(text)
    try:
        if parsed.scheme.lower() not in PROXY_SCHEMES or not parsed.hostname:
            return None
        # 端口写法非法（如 :abc）或越界时 urlparse 会在这里抛 ValueError
        if parsed.port is not None and parsed.port <= 0:
            return None
    except ValueError:
        return None
    return text


def normalize_region(raw: object) -> str | None:
    """规范化地区标签（小写）；空值或非法字符返回 None。"""
    text = raw.strip().lower() if isinstance(raw, str) else ""
    if not text or not _REGION_NAME.match(text):
        return None
    return text


@dataclass(frozen=True)
class ProxySpec:
    """池中的一条出口：地址 + 可选地区标签（如 us / hk / jp）+ 类别（dc / residential）。"""

    url: str
    region: str | None = None
    kind: str = _KIND_DEFAULT


def parse_entry(raw: object) -> ProxySpec | None:
    """解析单条池条目 ``url``、``url|region`` 或 ``url|region|kind``；非法返回 None。"""
    text = raw.strip() if isinstance(raw, str) else ""
    if not text:
        return None
    parts = text.split(_REGION_SEP)
    if len(parts) > 3:
        return None
    url = valid_proxy(parts[0])
    if url is None:
        return None
    region = normalize_region(parts[1]) if len(parts) > 1 else None
    kind = _KIND_DEFAULT
    if len(parts) == 3:
        kind = parts[2].strip().lower()
        # 类别写错就整条丢弃：静默当成 dc 会让"住宅升级"看起来配了却永远不生效
        if kind not in PROXY_KINDS:
            return None
    return ProxySpec(url, region, kind)


def parse_pool(raw: str) -> list[ProxySpec]:
    """解析代理列表：丢掉非法项，按 URL 去重且保持输入顺序（地区取首次出现的值）。"""
    specs: list[ProxySpec] = []
    seen: set[str] = set()
    for item in _SPLIT.split(raw or ""):
        spec = parse_entry(item)
        if spec is not None and spec.url not in seen:
            seen.add(spec.url)
            specs.append(spec)
    return specs


def split_pool(raw: str) -> list[str]:
    """只取地址部分（保留地区标签解析能力），供统计与旧调用方使用。"""
    return [spec.url for spec in parse_pool(raw)]


def parse_session_proxies(raw: str) -> dict[str, str]:
    """解析会话绑定代理：``name=proxy`` 条目，逗号/分号/空白分隔。

    非法会话名或非法代理条目直接忽略：配置写错不应让整个服务起不来，
    该会话会自然回落到代理池/全局配置。
    """
    mapping: dict[str, str] = {}
    for item in _SPLIT.split(raw or ""):
        name, sep, proxy = item.partition("=")
        if not sep:
            continue
        name = name.strip()
        url = valid_proxy(proxy)
        if url is None or not _SESSION_NAME.match(name):
            continue
        mapping[name] = url
    return mapping


def mask_proxy(raw: str) -> str:
    """把代理 URL 的账号密码替换为 ``***``，用于 notes / 日志脱敏。"""
    parsed = urlparse(raw)
    if not parsed.hostname:
        return "***"
    host = parsed.hostname
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    if parsed.username is not None or parsed.password is not None:
        host = f"***@{host}"
    return urlunparse(parsed._replace(netloc=host))


def _sticky_session(host: str) -> str:
    """按 host 派生稳定的 session id：同一站点在住宅出口上保持同一个 IP。"""
    return hashlib.sha1((host or "").encode("utf-8")).hexdigest()[:10]


def build_provider_proxy(
    template: str,
    *,
    user: str = "",
    password: str = "",
    host: str = "",
    region: str = "",
    sticky: bool = True,
    session: str | None = None,
) -> str | None:
    """展开住宅代理服务商模板；展开结果不是合法代理 URL 时返回 None。

    支持占位符 ``{user} {pass} {password} {host} {region} {session}``：
    - ``sticky=True``（默认）按 host 派生稳定 session，同站点复用同一出口；
    - ``sticky=False`` 每次调用生成新 session，即每请求轮换 IP。
    模板里出现未知占位符会展开失败并返回 None，由调用方提示配置错误。
    """
    text = (template or "").strip()
    if not text:
        return None
    if session is None:
        session = _sticky_session(host) if sticky else uuid.uuid4().hex[:10]
    values = {
        "user": user,
        "pass": password,
        "password": password,
        "host": host,
        "region": region or "",
        "session": session,
    }
    try:
        expanded = text.format_map(values)
    except (KeyError, ValueError, IndexError):
        return None
    return valid_proxy(expanded)


@dataclass
class _Entry:
    url: str
    region: str | None = None
    kind: str = _KIND_DEFAULT
    fails: int = 0
    cooldown_until: float = 0.0


class ProxyPool:
    """按站点粘性的轮询池：失败达阈值进入冷却，冷却期内不再分配。

    全部出口都在冷却时回退到最早解冻的那个——调用方本来就要发请求，
    宁可冒一次已知风险，也不要因为"池里没有健康出口"直接拒绝服务。
    指定地区时只在带该标签的出口中选择；没有可用条目返回 None，
    由调用方决定是报错还是回落到全局代理。
    """

    def __init__(
        self,
        urls: Iterable[str | ProxySpec],
        *,
        fail_threshold: int = 2,
        cooldown_s: float = 60.0,
        sticky_ttl_s: float = 300.0,
    ) -> None:
        self._entries = [_Entry(spec.url, spec.region, spec.kind) for spec in self._specs(urls)]
        self._fail_threshold = max(1, int(fail_threshold))
        self._cooldown_s = max(0.0, cooldown_s)
        self._sticky_ttl_s = max(0.0, sticky_ttl_s)
        # 绑定 key -> (代理, 绑定到期时刻)；key 为 host，指定地区时为 host|region。
        # 用单调时钟，不受系统时间调整影响
        self._bound: dict[str, tuple[str, float]] = {}
        self._cursor = 0

    @staticmethod
    def _specs(urls: Iterable[str | ProxySpec]) -> list[ProxySpec]:
        specs: list[ProxySpec] = []
        seen: set[str] = set()
        for item in urls:
            spec = item if isinstance(item, ProxySpec) else parse_entry(item)
            if spec is not None and spec.url not in seen:
                seen.add(spec.url)
                specs.append(spec)
        return specs

    def __bool__(self) -> bool:
        return bool(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def has_region(self, region: str) -> bool:
        """池中是否存在该地区的出口。"""
        return any(entry.region == region for entry in self._entries)

    def has_kind(self, kind: str) -> bool:
        """池中是否存在该类别的出口（住宅升级前用它判断是否有路可退）。"""
        return any(entry.kind == kind for entry in self._entries)

    def regions(self) -> list[str]:
        """池中已标注的全部地区（排序后），用于配置错误的提示信息。"""
        return sorted({e.region for e in self._entries if e.region})

    def pick(self, host: str, *, region: str | None = None, kind: str | None = None) -> str | None:
        """为本次请求选出口；池为空或指定地区/类别无出口时返回 None。"""
        candidates = self._entries
        key = host
        if kind is not None:
            candidates = [e for e in candidates if e.kind == kind]
        if region is not None:
            candidates = [e for e in candidates if e.region == region]
            key = f"{host}|{region}"
        if kind is not None:
            key = f"{key}|{kind}"
        if not candidates:
            return None
        now = time.monotonic()
        bound = self._bound.get(key)
        if bound is not None:
            url, expiry = bound
            entry = self._entry(url)
            if (
                expiry > now
                and entry is not None
                and entry in candidates
                and entry.cooldown_until <= now
            ):
                return url
            self._bound.pop(key, None)
        entry = self._next_available(now, candidates)
        self._bound[key] = (entry.url, now + self._sticky_ttl_s)
        return entry.url

    def report(self, proxy: str, *, ok: bool) -> None:
        """回报一次出口结果：成功清零计数，连续失败达阈值则进入冷却。"""
        entry = self._entry(proxy)
        if entry is None:
            return
        if ok:
            entry.fails = 0
            return
        entry.fails += 1
        if entry.fails < self._fail_threshold:
            return
        entry.fails = 0
        entry.cooldown_until = time.monotonic() + self._cooldown_s
        # 冷却中的出口不再承接粘性站点，下次请求重新分配
        self._bound = {k: v for k, v in self._bound.items() if v[0] != proxy}

    def snapshot(self) -> dict[str, int]:
        """池状态摘要；不含代理地址，避免凭据进入统计与日志。

        除总量/健康外给出按类别（dc / residential）的计数，
        便于确认"住宅升级"是否真的配了备用出口。
        """
        now = time.monotonic()
        cooling = sum(1 for e in self._entries if e.cooldown_until > now)
        return {
            "size": len(self._entries),
            "healthy": len(self._entries) - cooling,
            "cooling": cooling,
            "dc": sum(1 for e in self._entries if e.kind == "dc"),
            "residential": sum(1 for e in self._entries if e.kind == "residential"),
        }

    def _entry(self, url: str) -> _Entry | None:
        for entry in self._entries:
            if entry.url == url:
                return entry
        return None

    def _next_available(self, now: float, entries: list[_Entry]) -> _Entry:
        for i in range(len(entries)):
            idx = (self._cursor + i) % len(entries)
            entry = entries[idx]
            if entry.cooldown_until <= now:
                self._cursor = (idx + 1) % len(entries)
                return entry
        return min(entries, key=lambda e: e.cooldown_until)


__all__ = [
    "PROXY_KINDS",
    "PROXY_SCHEMES",
    "ProxyPool",
    "ProxySpec",
    "build_provider_proxy",
    "mask_proxy",
    "normalize_region",
    "parse_entry",
    "parse_pool",
    "parse_session_proxies",
    "split_pool",
    "valid_proxy",
]
