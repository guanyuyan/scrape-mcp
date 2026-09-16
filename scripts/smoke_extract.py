"""HTTP 冒烟测试：对运行中的 streamable-http 服务发一次 web_extract。

用法：
    1) 先按 README 启动服务（streamable-http 模式）
    2) python scripts/smoke_extract.py [--url <页面>] [--port 8000]

依赖：仅标准库（urllib），复用 scripts/smoke_http.call。
"""

from __future__ import annotations

import argparse
import sys

from smoke_http import call


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="https://developer.mozilla.org/zh-CN/docs/Web/HTTP/CORS")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()

    schema = {
        "fields": {
            "heading": {"selector": "h1", "type": "text"},
            "og_title": {"selector": "meta[property='og:title']", "type": "attr", "attr": "content"},
            "h2_list": {"selector": "h2", "type": "list", "list_key": "text"},
            "code_blocks": {"selector": "pre code", "type": "count"},
            "edit_link": {"selector": "a[href*='edit']", "type": "attr", "attr": "href"},
        }
    }

    print(f"[smoke] POST http://127.0.0.1:{args.port}/mcp  web_extract {args.url}")
    r = call(args.port, "web_extract", {"url": args.url, "schema": schema})

    print(
        f"[smoke] ok={r.get('ok')} tier={r.get('tier')} status={r.get('status')} "
        f"missing={r.get('missing')} elapsed_ms={r.get('elapsed_ms')}"
    )
    if not r.get("ok"):
        print(f"[smoke] error={r.get('error')} blocked={r.get('blocked')} login_required={r.get('login_required')}")
        return 2
    print(f"[smoke] heading={r.get('data', {}).get('heading')!r}")
    print(f"[smoke] og_title={r.get('data', {}).get('og_title')!r}")
    print(f"[smoke] h2 前 3 个: {r.get('data', {}).get('h2_list')[:3]}")
    print(f"[smoke] code_blocks={r.get('data', {}).get('code_blocks')} edit_link={r.get('data', {}).get('edit_link')!r}")
    print("[smoke] OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())