"""robots.txt 合规检查（阶段 0）：默认遵循目标站点的抓取规则。

只做"路径是否允许"的判定；规则获取失败（网络错误、无 robots.txt）一律视为允许，
保证合规开关不会因规则拿不到而降级可用性。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from curl_cffi.requests import AsyncSession

_UA = "scrape-mcp/0.1 (+robots-fetch)"

# User-agent 分组里命中这些关键词即视为"本爬虫适用"；否则仅精确匹配我们声明的名字
_OUR_TOKENS = ("scrape-mcp", "scrape_mcp", "scraper")


@dataclass
class _Rule:
    allow: bool
    pattern: str


class RobotsChecker:
    """拉取并缓存各站 robots.txt，按 robots 规范用最长前缀匹配判定路径。"""

    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._cache: dict[str, tuple[float, list[_Rule] | None]] = {}

    async def is_allowed(self, url: str) -> bool:
        _, rules = await self._rules_for(url)
        return self._match(rules, urlparse(url).path)

    async def _rules_for(self, url: str) -> tuple[str, list[_Rule] | None]:
        host = (urlparse(url).netloc or "").lower()
        if not host:
            return "", None
        cached = self._cache.get(host)
        if cached is not None and time.time() - cached[0] < self._settings.robots_cache_ttl:
            return host, cached[1]
        rules = await self._fetch_rules(host)
        self._cache[host] = (time.time(), rules)
        return host, rules

    async def _fetch_rules(self, host: str) -> list[_Rule] | None:
        robots_url = f"https://{host}/robots.txt"
        try:
            async with AsyncSession(impersonate=self._settings.impersonate) as session:
                resp = await session.get(
                    robots_url,
                    headers={"User-Agent": _UA},
                    timeout=min(8.0, self._settings.request_timeout),
                    allow_redirects=True,
                )
        except Exception:  # robot 判定是系统边界：拿不到规则就放行，不阻断可用性
            return None
        if resp.status_code == 404:
            return None
        if resp.status_code not in (200, 401, 403):
            return None
        return _parse_robots(getattr(resp, "text", "") or "")

    @staticmethod
    def _match(rules: list[_Rule] | None, path: str) -> bool:
        """最长前缀匹配；定义域不同的路径前缀比较长度取最长，平局时 allow 优先。

        无规则匹配（拿不到规则、空规则、路径不命中任何 Disallow）一律放行。
        """
        if not rules:
            return True
        matched = False
        allowed = False
        best_len = -1
        for r in rules:
            if path.startswith(r.pattern) and len(r.pattern) > best_len:
                best_len, allowed, matched = len(r.pattern), r.allow, True
            elif path.startswith(r.pattern) and len(r.pattern) == best_len and r.allow:
                allowed = matched = True
        return allowed if matched else True


def _parse_robots(text: str) -> list[_Rule] | None:
    rules: list[_Rule] = []
    applicable = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "#" in line:
            line = line.split("#", 1)[0].strip()
        key, _, val = line.partition(":")
        key, val = key.strip().lower(), val.strip()
        if not val:
            continue
        if key == "user-agent":
            applicable = _agent_applies(val)
        elif key == "allow" and applicable:
            rules.append(_Rule(True, val))
        elif key == "disallow" and applicable:
            rules.append(_Rule(False, val))
    return rules or None


def _agent_applies(agent: str) -> bool:
    agent = agent.strip().lower()
    if agent == "*":
        return True
    return any(tok in agent for tok in _OUR_TOKENS)
