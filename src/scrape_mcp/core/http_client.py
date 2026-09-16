"""L1 抓取层：curl_cffi，伪装 TLS/JA3 与 HTTP2 指纹，成本最低。"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from curl_cffi.requests import AsyncSession

from ..config import Settings
from .cache import HttpCache

_META_CHARSET = re.compile(rb'charset\s*=\s*["\']?\s*([\w-]+)', re.I)
_HEADER_CHARSET = re.compile(r"charset\s*=\s*([\w-]+)", re.I)


@dataclass
class HttpResponse:
    ok: bool
    status: int
    url: str
    content_type: str
    text: str
    elapsed_ms: int
    error: str | None = None
    cached: bool = False


def _decode(raw: bytes, content_type: str) -> str:
    charset = ""
    m = _HEADER_CHARSET.search(content_type or "")
    if m:
        charset = m.group(1)
    if not charset:
        m = _META_CHARSET.search(raw[:4096])
        if m:
            charset = m.group(1).decode("ascii", "ignore")
    for enc in (charset, "utf-8", "gb18030"):
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


class HttpClient:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._cache = HttpCache(settings.data_dir, settings.cache_ttl)

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        impersonate: str | None = None,
        timeout: float | None = None,
        proxy: str | None = None,
    ) -> HttpResponse:
        saved = self._cache.get(url)
        if saved is not None:
            return HttpResponse(
                ok=True,
                status=saved.get("status", 200),
                url=saved.get("url", url),
                content_type=saved.get("content_type", ""),
                text=saved.get("text", ""),
                elapsed_ms=0,
                cached=True,
            )

        t0 = time.perf_counter()
        merged = {"Accept-Language": self._s.accept_language}
        if headers:
            merged.update(headers)
        try:
            async with AsyncSession(impersonate=impersonate or self._s.impersonate) as session:
                resp = await session.get(
                    url,
                    headers=merged,
                    cookies=cookies or None,
                    timeout=timeout or self._s.request_timeout,
                    proxy=proxy or self._s.proxy,
                    allow_redirects=True,
                )
        except Exception as exc:  # 网络层是系统边界，需要兜住所有传输异常
            return HttpResponse(
                ok=False,
                status=0,
                url=url,
                content_type="",
                text="",
                elapsed_ms=int((time.perf_counter() - t0) * 1000),
                error=f"{type(exc).__name__}: {exc}",
            )

        raw = resp.content or b""
        if len(raw) > self._s.max_response_bytes:
            raw = raw[: self._s.max_response_bytes]
        content_type = resp.headers.get("content-type", "") or ""
        text = _decode(raw, content_type)
        out = HttpResponse(
            ok=True,
            status=resp.status_code,
            url=str(resp.url) or url,
            content_type=content_type,
            text=text,
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
        )
        # 只缓存可判定的正常 HTML；拦截/错误响应不缓存，避免把挑战页当作有效内容缓存
        if out.status == 200 and "text/html" in content_type:
            self._cache.put(
                url,
                {"status": out.status, "url": out.url, "content_type": content_type, "text": text},
            )
        return out
