"""CLI 子命令：客户端配置生成（init）与工具定义导出（schema）。

主入口仍在 ``scrape_mcp.server:main``：不带子命令时保持零配置 stdio 行为，
只有显式写 ``scrape-mcp init`` / ``scrape-mcp schema`` 才走这里。
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SERVER_NAME = "scrape-mcp"
CLIENTS = ("claude", "cursor", "vscode", "json")
SCOPES = ("global", "project")

# 各客户端的配置文件名与容器键：Claude/Cursor 用 mcpServers，VS Code 用 servers
_CLIENT_SPECS: dict[str, dict[str, str]] = {
    "claude": {"root_key": "mcpServers", "entry_type": ""},
    "cursor": {"root_key": "mcpServers", "entry_type": ""},
    "json": {"root_key": "mcpServers", "entry_type": ""},
    "vscode": {"root_key": "servers", "entry_type": "stdio"},
}


def _claude_config_path() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        home = Path(base) if base else Path.home() / "AppData" / "Roaming"
        return home / "Claude" / "claude_desktop_config.json"
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def _target_path(client: str, scope: str, cwd: Path) -> Path | None:
    """返回配置文件路径；json（只打印）没有落盘位置。"""
    if client == "json":
        return None
    if client == "claude":
        # Claude Desktop 只读全局配置，不存在项目级
        return _claude_config_path()
    if client == "cursor":
        base = Path.home() / ".cursor" if scope == "global" else cwd / ".cursor"
        return base / "mcp.json"
    return cwd / ".vscode" / "mcp.json"


def _command(argv: str | None) -> tuple[str, list[str]]:
    """定位启动命令：优先调用方指定，其次安装后的命令行入口，最后退回当前解释器。"""
    if argv:
        return argv, []
    found = shutil.which(SERVER_NAME)
    if found:
        return found, []
    return sys.executable, ["-m", "scrape_mcp.server"]


def _entry(client: str, command: str, args: list[str], env: dict[str, str]) -> dict[str, Any]:
    entry: dict[str, Any] = {}
    entry_type = _CLIENT_SPECS[client]["entry_type"]
    if entry_type:
        entry["type"] = entry_type
    entry["command"] = command
    entry["args"] = args
    if env:
        entry["env"] = dict(sorted(env.items()))
    return entry


def _parse_env(pairs: list[str] | None) -> dict[str, str]:
    env: dict[str, str] = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"--env 需要 KEY=VALUE 形式，收到 {pair!r}")
        env[key.strip()] = value
    return env


def _load_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("配置文件顶层不是对象")
    return data


def _render(config: dict[str, Any]) -> str:
    return json.dumps(config, ensure_ascii=False, indent=2) + "\n"


def _diff(before: str, after: str, path: str) -> str:
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"{path}（当前）",
        tofile=f"{path}（写入后）",
    )
    return "".join(lines)


def _build_config(
    client: str,
    existing: dict[str, Any],
    command: str,
    args: list[str],
    env: dict[str, str],
    *,
    force: bool,
) -> tuple[dict[str, Any], bool]:
    """把条目合并进已有配置，返回 (新配置, 是否覆盖了已有同名条目)。"""
    root_key = _CLIENT_SPECS[client]["root_key"]
    servers = existing.get(root_key)
    if not isinstance(servers, dict):
        servers = {}
    replaced = SERVER_NAME in servers
    if replaced and not force:
        return existing, True
    merged = dict(existing)
    merged[root_key] = {**servers, SERVER_NAME: _entry(client, command, args, env)}
    return merged, replaced


def _cmd_init(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="scrape-mcp init",
        description="生成 MCP 客户端配置；默认只打印 diff，加 --write 才落盘。",
    )
    parser.add_argument("--client", choices=CLIENTS, default="claude", help="目标客户端")
    parser.add_argument("--scope", choices=SCOPES, default="global", help="global 或 project")
    parser.add_argument("--command", help="覆盖启动命令（默认自动定位 scrape-mcp）")
    parser.add_argument(
        "--env", action="append", metavar="KEY=VALUE", help="写入 client 配置的环境变量，可重复"
    )
    parser.add_argument("--path", help="覆盖配置文件路径（调试或自定义安装时用）")
    parser.add_argument("--write", action="store_true", help="实际写入；缺省只预览")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的同名条目")
    args = parser.parse_args(argv)

    try:
        env = _parse_env(args.env)
    except ValueError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2

    path = (
        Path(args.path).expanduser()
        if args.path
        else _target_path(args.client, args.scope, Path.cwd())
    )
    command, cmd_args = _command(args.command)
    existing: dict[str, Any] = {}
    if path is not None:
        try:
            existing = _load_config(path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            if not args.force:
                print(f"[错误] 读取 {path} 失败: {exc}（确认后可加 --force 覆盖）", file=sys.stderr)
                return 2
            existing = {}
        before = _render(existing)
    else:
        before = ""

    config, replaced = _build_config(
        args.client, existing, command, cmd_args, env, force=args.force
    )
    after = _render(config)

    if path is not None and replaced and not args.force:
        print(f"[提示] {path} 里已有 {SERVER_NAME} 条目，未做改动；需要更新请加 --force")
        return 1

    if path is None:
        sys.stdout.write(after)
        return 0

    if before == after:
        print(f"[完成] {path} 已是最新，无需改动")
        return 0

    sys.stdout.write(_diff(before, after, str(path)))
    if not args.write:
        print(f"\n[预览] 未写入。确认无误后执行：scrape-mcp init ... --write（目标 {path}）")
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(after, encoding="utf-8")
    print(f"\n[完成] 已写入 {path}；重启客户端后生效")
    return 0


def _url_host(url: str) -> str | None:
    """与 Server 侧一致的主机口径：netloc 小写、去掉端口，仅认 http/https。"""
    if not url.startswith(("http://", "https://")):
        return None
    return (urlparse(url).netloc or "").lower().split(":")[0] or None


def _cmd_session(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="scrape-mcp session",
        description="查看或导入登录态；导入只读本地文件，不需要浏览器。",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list", help="列出已保存的登录态（会话、主机、更新时间）")
    importer = sub.add_parser("import", help="从 JSON 或 Netscape cookies.txt 导入 cookie")
    importer.add_argument("--url", required=True, help="目标站点任一页面 URL")
    importer.add_argument("--file", required=True, help="cookie 文件路径")
    importer.add_argument("--session", default="default", help="会话名，默认 default")
    importer.add_argument("--replace", action="store_true", help="整体替换该会话的 cookie")
    args = parser.parse_args(argv)

    from .config import get_settings
    from .core.session import CookieFileError, SessionStore, load_cookie_file, normalize_session

    store = SessionStore(get_settings().data_dir)
    if args.action == "list":
        items = store.list_sessions()
        if not items:
            print("[空] 还没有保存的登录态；可用 scrape-mcp session import 导入 cookies")
            return 0
        for item in items:
            print(f"{item['session']}\t{item['host']}\t{item['updated_at']}")
        return 0

    host = _url_host(args.url)
    if host is None:
        print("[错误] --url 必须以 http:// 或 https:// 开头", file=sys.stderr)
        return 2
    try:
        session = normalize_session(args.session)
    except ValueError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2

    source = Path(args.file).expanduser()
    try:
        cookies = load_cookie_file(source)
    except (OSError, UnicodeDecodeError) as exc:
        print(f"[错误] 读取 {source} 失败: {exc}", file=sys.stderr)
        return 2
    except CookieFileError as exc:
        print(f"[错误] {source} 无法解析: {exc}", file=sys.stderr)
        return 2

    count = store.import_cookies(host, cookies, session, replace=args.replace)
    if count == 0:
        print("[错误] 文件里没有可用的 cookie（至少需要 name 与 value）", file=sys.stderr)
        return 1
    mode = "替换" if args.replace else "合并"
    print(f"[完成] 已{mode}导入 {count} 条 cookie → {host}（会话 {session}）")
    return 0


def _tool_record(tool: Any) -> dict[str, Any]:
    """把 SDK 的 MCPTool 对象转成稳定的 JSON 结构，只保留对外声明的字段。"""
    record: dict[str, Any] = {
        "name": tool.name,
        "description": tool.description or "",
        "input_schema": tool.input_schema,
    }
    output_schema = getattr(tool, "output_schema", None)
    if output_schema:
        record["output_schema"] = output_schema
    annotations = getattr(tool, "annotations", None)
    if annotations is not None:
        record["annotations"] = annotations.model_dump(exclude_none=True)
    return record


def _cmd_schema(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="scrape-mcp schema",
        description="导出工具定义（名称、说明、入参 schema），便于生成客户端与文档。",
    )
    parser.add_argument("--tool", help="只导出指定工具")
    parser.add_argument("--output", help="写入文件；缺省打印到 stdout")
    args = parser.parse_args(argv)

    from . import __version__

    tools = asyncio.run(_list_tools())
    records = [_tool_record(t) for t in tools]
    if args.tool:
        records = [r for r in records if r["name"] == args.tool]
        if not records:
            print(f"[错误] 没有名为 {args.tool} 的工具", file=sys.stderr)
            return 2

    payload = {"server": SERVER_NAME, "version": __version__, "tools": records}
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        target = Path(args.output).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        print(f"[完成] 已导出 {len(records)} 个工具到 {target}")
        return 0
    sys.stdout.write(text)
    return 0


async def _list_tools() -> list[Any]:
    from .server import mcp

    return await mcp.list_tools()


def run(argv: list[str]) -> int:
    """执行子命令；未知子命令给出用法提示。"""
    if not argv:
        print("用法: scrape-mcp <init|schema|session> ...", file=sys.stderr)
        return 2
    command, rest = argv[0], argv[1:]
    if command == "init":
        return _cmd_init(rest)
    if command == "schema":
        return _cmd_schema(rest)
    if command == "session":
        return _cmd_session(rest)
    print(f"未知子命令: {command}\n用法: scrape-mcp <init|schema|session> ...", file=sys.stderr)
    return 2
