"""阶段 0 合规测试：robots 解析、黑白名单、限流。"""

import asyncio

from scrape_mcp.core.ratelimit import RateLimiter
from scrape_mcp.core.robots import RobotsChecker, _agent_applies, _parse_robots


class _Settings:
    impersonate = "chrome"
    request_timeout = 20.0
    robots_cache_ttl = 86400.0


def _match(rules, path):
    return RobotsChecker._match(rules, path)


def test_agent_applies():
    assert _agent_applies("*")
    assert _agent_applies("  Scrape-MCP/1.0  ")
    assert _agent_applies("MyScraperBot")
    assert not _agent_applies("Googlebot")


def test_robots_allow_most_specific_wins():
    text = (
        "User-agent: *\n"
        "Disallow: /\n"
        "User-agent: Scrape-MCP\n"
        "Allow: /public\n"
    )
    # 通用组 Disallow /，特定组 Allow /public——/public 应被最长前缀命中为允许
    assert _match(_parse_robots(text), "/public/page")


def test_robots_disallow_takes_effect():
    text = "User-agent: *\nDisallow: /private\n"
    assert _match(_parse_robots(text), "/private") is False
    assert _match(_parse_robots(text), "/private/secret") is False
    # 未命中任何 Disallow 的路径默认放行
    assert _match(_parse_robots(text), "/public") is True


def test_robots_no_rules_means_allow():
    assert _match(None, "/anything") is True
    assert _match([], "/anything") is True


def test_robots_allow_precedes_disallow_on_tie():
    # 同路径，Allow 后于 Disallow 出现——robots 规范平局时 Allow 优先
    text = "User-agent: *\nDisallow: /x\nAllow: /x\n"
    assert _match(_parse_robots(text), "/x") is True


def test_robots_checker_network_error_is_allow():
    """网络异常/拿不到 robots.txt 应放行，不掉可用性。"""

    async def fake_get(*args, **kwargs):
        raise RuntimeError("no network")

    async def run():
        checker = RobotsChecker(_Settings())
        checker._cache.clear()
        import scrape_mcp.core.robots as rm

        original = rm.AsyncSession
        try:
            rm.AsyncSession = _FakeAsync
            result = await checker.is_allowed("https://example.com/a")
        finally:
            rm.AsyncSession = original
        return result

    class _FakeAsync:
        """模拟会话上下文管理器。"""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kwargs):
            await fake_get()

    assert asyncio.run(run()) is True


def test_ratelimiter_blocks_surge():
    async def run():
        lim = RateLimiter(max_qps=100.0)
        start = asyncio.get_event_loop().time()
        await asyncio.gather(*(lim.acquire() for _ in range(200)))
        return asyncio.get_event_loop().time() - start

    assert asyncio.run(run()) >= 1.0


def test_ratelimiter_zero_is_noop():
    async def run():
        lim = RateLimiter(max_qps=0.0)
        await asyncio.gather(*(lim.acquire() for _ in range(50)))
        return True

    assert asyncio.run(run()) is True
