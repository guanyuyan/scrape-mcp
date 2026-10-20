"""L1 抓取层：curl_cffi，伪装 TLS/JA3 与 HTTP2 指纹，成本最低。"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

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
    # 服务端要求的下次请求间隔（秒），来自 Retry-After；没有则为 None
    retry_after: float | None = None
    # 原始响应体（PDF 等二进制内容需要原样交给上层解析）
    content: bytes = b""


def _parse_retry_after(raw: str) -> float | None:
    """Retry-After 支持两种写法：秒数，或 HTTP 日期。解析不了就返回 None。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


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
        bypass_cache: bool = False,
    ) -> HttpResponse:
        # 带自定义头/凭证的响应可能因人而异，既不读也不写共享缓存
        use_cache = not bypass_cache and not headers and not cookies
        saved = None
        if use_cache:
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
        # 过期缓存不直接丢：带 ETag/Last-Modified 去问一次，304 就复用正文
        stale = self._cache.get(url, allow_stale=True) if use_cache else None
        if stale:
            if stale.get("etag"):
                merged["If-None-Match"] = stale["etag"]
            if stale.get("last_modified"):
                merged["If-Modified-Since"] = stale["last_modified"]
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

        resp_headers = {k.lower(): v for k, v in (resp.headers or {}).items()}
        content_type = resp_headers.get("content-type", "") or ""

        # 304：内容未变，直接复用旧正文，并刷新缓存时间戳
        if resp.status_code == 304 and stale is not None:
            self._cache.put(url, stale)
            return HttpResponse(
                ok=True,
                status=stale.get("status", 200),
                url=stale.get("url", url),
                content_type=stale.get("content_type", content_type),
                text=stale.get("text", ""),
                elapsed_ms=int((time.perf_counter() - t0) * 1000),
                cached=True,
                retry_after=_parse_retry_after(resp_headers.get("retry-after", "")),
            )

        raw = resp.content or b""
        # PDF 是二进制，截断后无法解析；其余内容仍按上限截断，避免大页面吃内存
        is_pdf = "application/pdf" in content_type.lower()
        if not is_pdf and len(raw) > self._s.max_response_bytes:
            raw = raw[: self._s.max_response_bytes]
        text = _decode(raw, content_type)
        out = HttpResponse(
            ok=True,
            status=resp.status_code,
            url=str(resp.url) or url,
            content_type=content_type,
            text=text,
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
            retry_after=_parse_retry_after(resp_headers.get("retry-after", "")),
            content=raw,
        )
        # 只缓存可判定的正常 HTML；拦截/错误响应不缓存，避免把挑战页当作有效内容缓存
        if use_cache and out.status == 200 and "text/html" in content_type:
            self._cache.put(
                url,
                {
                    "status": out.status,
                    "url": out.url,
                    "content_type": content_type,
                    "text": text,
                    "etag": resp_headers.get("etag", ""),
                    "last_modified": resp_headers.get("last-modified", ""),
                },
            )
        return out
