"""HTTP 冒烟测试：对运行中的 streamable-http 服务发一次 web_fetch。

用法：
    1) 先按 README 启动服务（streamable-http 模式）
    2) python scripts/smoke_http.py [--url <页面>] [--port 8000]

依赖：仅标准库（urllib）。返回 JSON 已由服务端压成一行，
这里直接打印关键字段，非零退出码表示异常。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request

PROTO = "2026-07-28"


def call(port: int, name: str, arguments: dict) -> dict:
    url = f"http://127.0.0.1:{port}/mcp"
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": arguments,
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": PROTO,
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            },
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTO,
            "MCP-Method": "tools/call",
            "Mcp-Name": name,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8")
    # 可能是单条 JSON，也可能是 SSE（data: 行）。这里只处理纯 JSON 响应。
    try:
        outer = json.loads(raw)
    except json.JSONDecodeError:
        lines = [ln for ln in raw.splitlines() if ln.startswith("data: ")]
        outer = json.loads(lines[-1][6:]) if lines else {}
    return json.loads(outer["result"]["content"][0]["text"])


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="https://developer.mozilla.org/zh-CN/docs/Web/HTTP/CORS")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()

    print(f"[smoke] POST http://127.0.0.1:{args.port}/mcp  web_fetch {args.url}")
    r = call(args.port, "web_fetch", {"url": args.url, "max_tokens": 2000})

    print(
        f"[smoke] ok={r.get('ok')} tier={r.get('tier')} status={r.get('status')} "
        f"tokens={r.get('token_estimate')} truncated={r.get('truncated')} elapsed_ms={r.get('elapsed_ms')}"
    )
    if not r.get("ok"):
        print(
            f"[smoke] error={r.get('error')} blocked={r.get('blocked')} login_required={r.get('login_required')}"
        )
        return 2
    print(f"[smoke] title={r.get('title')!r}")
    print("[smoke] content 前 120 字:\n" + (r.get("content") or "")[:120])
    print("[smoke] OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
