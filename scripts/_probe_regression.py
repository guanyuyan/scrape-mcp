"""临时回归探针：真实站点未截断口径验证（不提交）。"""

from __future__ import annotations

import asyncio
import time

from scrape_mcp.config import get_settings
from scrape_mcp.core.detector import looks_like_shell
from scrape_mcp.core.fetcher import Fetcher
from scrape_mcp.extract.compact import compact_html

_SITES = [
    ("MDN /CORS", "https://developer.mozilla.org/zh-CN/docs/Web/HTTP/CORS"),
    ("博客园文章", "https://www.cnblogs.com/wang_yb/p/22981033"),
    ("澎湃文章", "https://www.thepaper.cn/newsDetail_forward_23443163"),
    ("网易首页", "https://www.163.com/"),
    ("新浪首页", "https://www.sina.com.cn/"),
]


async def probe() -> None:
    fetcher = Fetcher(get_settings())
    for name, url in _SITES:
        t0 = time.perf_counter()
        try:
            o = await fetcher.fetch(url)
            ms = int((time.perf_counter() - t0) * 1000)
            if o.error or o.blocked != "none":
                print(f"[{name}] ERR tier={o.tier} blocked={o.blocked} err={o.error!r} ({ms}ms)")
                continue
            r = compact_html(o.html, o.final_url or url, link_policy="internal", max_tokens=10**9)
            print(
                f"[{name}] tier={o.tier} status={o.status} "
                f"chars={len(r.content)} tokens={len(r.content) // 2} "
                f"blocks={r.blocks} links_kept={r.links_kept} links_dropped={r.links_dropped} "
                f"truncated={r.truncated} shell={looks_like_shell(o.html)} ({ms}ms)"
            )
        except Exception as exc:
            print(f"[{name}] EXC {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(probe())
