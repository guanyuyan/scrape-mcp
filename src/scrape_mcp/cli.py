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
import tempfile
from datetime import datetime
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


def _recipe_entries(directory: Path) -> list[tuple[Path, dict[str, Any]]]:
    """读目录里的 *.json；坏文件不抛错，交给调用方登记为问题。"""
    entries: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            data = {}
        entries.append((path, data if isinstance(data, dict) else {}))
    return entries


def _recipe_summary(data: dict[str, Any]) -> str:
    """一行摘要：只列真正会改变抓取行为的开关。"""
    parts: list[str] = []
    for key, label in (("root_selector", "root"), ("wait_for", "wait"), ("click", "click")):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(f"{label}={value.strip()}")
    if data.get("scroll") is True:
        parts.append("scroll")
    extract = data.get("extract")
    if isinstance(extract, dict):
        fields = extract.get("fields") if isinstance(extract.get("fields"), dict) else extract
        if isinstance(fields, dict) and fields:
            parts.append(f"extract={len(fields)} 字段")
    crawl = data.get("crawl") if isinstance(data.get("crawl"), dict) else {}
    if crawl:
        next_selector = crawl.get("next_selector")
        pages = crawl.get("max_pages")
        bits = []
        if isinstance(next_selector, str) and next_selector.strip():
            bits.append(next_selector.strip())
        if isinstance(pages, int) and pages > 0:
            bits.append(f"≤{pages} 页")
        if bits:
            parts.append("crawl=" + " ".join(bits))
    return " · ".join(parts) or "（仅按主机匹配，不改行为）"


def _cmd_recipe(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="scrape-mcp recipe",
        description="查看与校验站点 recipe（JSON）：先本地验结构，再放进 recipe 目录。",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    lister = sub.add_parser("list", help="列出目录里的 recipe（生效主机、文件、启用项）")
    lister.add_argument("--dir", help="recipe 目录；缺省为 <DATA_DIR>/recipes")
    checker = sub.add_parser("validate", help="校验结构、选择器语法与抽取 schema")
    checker.add_argument("paths", nargs="*", help="要校验的 .json 文件或目录；缺省校验默认目录")
    checker.add_argument("--strict", action="store_true", help="把警告也视为失败")
    exporter = sub.add_parser("export", help="把本地 recipe 打包成可分享的 bundle")
    exporter.add_argument("hosts", nargs="*", help="只导出这些 host；缺省导出目录里全部")
    exporter.add_argument("--dir", help="recipe 目录；缺省为 <DATA_DIR>/recipes")
    exporter.add_argument("-o", "--out", help="输出文件；缺省打印到 stdout")
    importer = sub.add_parser("import", help="导入 bundle 到 recipe 目录")
    importer.add_argument("bundle", help="bundle 的 .json 路径")
    importer.add_argument("--dir", help="recipe 目录；缺省为 <DATA_DIR>/recipes")
    importer.add_argument("--overwrite", action="store_true", help="覆盖同名但内容不同的 recipe")
    importer.add_argument("--dry-run", action="store_true", help="只打印计划，不写文件")
    args = parser.parse_args(argv)

    from .core.recipe_bundle import (
        BundleImportResult,
        RecipeBundleError,
        build_bundle,
        import_bundle,
    )
    from .core.recipes import (
        RecipeIssue,
        default_recipe_dir,
        recipe_host,
        validate_recipe_file,
    )

    if args.action == "export":
        directory = Path(args.dir).expanduser() if args.dir else default_recipe_dir()
        if not directory.is_dir():
            print(f"[空] 目录不存在：{directory}（可用 RECIPES_DIR 或 --dir 指定）")
            return 0
        bundle, warnings = build_bundle(directory, list(args.hosts))
        for warning in warnings:
            print(f"[警告] {warning}", file=sys.stderr)
        text = json.dumps(bundle, ensure_ascii=False, indent=2) + "\n"
        if args.out:
            target = Path(args.out).expanduser()
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
            except OSError as exc:
                print(f"[错误] 写入失败：{exc}", file=sys.stderr)
                return 2
            print(f"[完成] 导出 {len(bundle['recipes'])} 个 recipe → {target}")
        else:
            sys.stdout.write(text)
            print(f"[完成] 导出 {len(bundle['recipes'])} 个 recipe", file=sys.stderr)
        missing = any("未找到 host" in warning for warning in warnings)
        return 1 if missing else 0

    if args.action == "import":
        source = Path(args.bundle).expanduser()
        try:
            data = json.loads(source.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError) as exc:
            print(f"[错误] 读取失败：{exc}", file=sys.stderr)
            return 2
        except json.JSONDecodeError as exc:
            print(
                f"[错误] JSON 解析失败：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）",
                file=sys.stderr,
            )
            return 1
        directory = Path(args.dir).expanduser() if args.dir else default_recipe_dir()
        try:
            result: BundleImportResult = import_bundle(
                data, directory, overwrite=args.overwrite, dry_run=args.dry_run
            )
        except RecipeBundleError as exc:
            print(f"[错误] bundle 不可用：{source}", file=sys.stderr)
            for issue in exc.issues:
                print(f"  {issue}", file=sys.stderr)
            return 1
        label = "[预览]" if args.dry_run else "[写入]"
        for path in result.written:
            print(f"{label} {path.name}")
        for path in result.identical:
            print(f"[相同] {path.name}")
        for path in result.conflicts:
            print(f"[冲突] {path.name}（已存在且内容不同，加 --overwrite 覆盖）")
        summary = (
            f"写入 {len(result.written)} 个，相同 {len(result.identical)} 个，"
            f"冲突 {len(result.conflicts)} 个（{directory}）"
        )
        if result.conflicts:
            print(f"\n[失败] {summary}", file=sys.stderr)
            return 1
        print(f"\n[完成] {summary}")
        return 0

    if args.action == "list":
        directory = Path(args.dir).expanduser() if args.dir else default_recipe_dir()
        if not directory.is_dir():
            print(f"[空] 目录不存在：{directory}（可用 RECIPES_DIR 或 --dir 指定）")
            return 0
        entries = _recipe_entries(directory)
        if not entries:
            print(f"[空] {directory} 下没有 .json recipe")
            return 0
        width = max((len(recipe_host(data, path.stem)) for path, data in entries), default=0)
        for path, data in entries:
            host = recipe_host(data, path.stem)
            print(f"{host:<{width}}  {path.name:<28}  {_recipe_summary(data)}")
        print(f"\n共 {len(entries)} 个 recipe（{directory}）")
        return 0

    targets = [Path(item).expanduser() for item in args.paths] or [default_recipe_dir()]
    files: list[Path] = []
    for target in targets:
        if target.is_dir():
            found = sorted(target.glob("*.json"))
            if not found:
                print(f"[空] {target} 下没有 .json recipe")
            files.extend(found)
        elif target.is_file():
            files.append(target)
        else:
            print(f"[错误] 找不到 {target}", file=sys.stderr)
            return 2
    if not files:
        return 0

    errors = warnings = 0
    seen: dict[str, Path] = {}
    for path in files:
        issues = validate_recipe_file(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            host = recipe_host(data, path.stem)
            first = seen.get(host)
            if first is not None:
                issues.append(
                    RecipeIssue(
                        "warning",
                        f"host {host} 与 {first.name} 重复；加载时按目录顺序以最后一个为准",
                    )
                )
            else:
                seen[host] = path
        if not issues:
            continue
        print(f"[文件] {path}")
        for issue in issues:
            print(f"  {issue}")
            if issue.level == "error":
                errors += 1
            else:
                warnings += 1

    summary = f"{len(files)} 个文件，{errors} 个错误、{warnings} 个警告"
    if errors:
        print(f"\n[失败] {summary}", file=sys.stderr)
        return 1
    if warnings and args.strict:
        print(f"\n[失败] {summary}（--strict 把警告视为失败）", file=sys.stderr)
        return 1
    print(f"\n[完成] {summary}")
    return 0


def _cmd_dataset(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="scrape-mcp dataset",
        description="查看或导出抓取数据集：设置 SCRAPE_MCP_DATASET=<path>.jsonl 后自动累积。",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    stats = sub.add_parser("stats", help="概况：条数、按工具分布、时间跨度、平均体量")
    stats.add_argument("path", nargs="?", help="JSONL 路径；缺省取 SCRAPE_MCP_DATASET")
    parquet = sub.add_parser(
        "to-parquet", help='转成 Parquet（需 pip install "mcp-scrape[dataset]"）'
    )
    parquet.add_argument("path", nargs="?", help="JSONL 路径；缺省取 SCRAPE_MCP_DATASET")
    parquet.add_argument("-o", "--out", help="输出 .parquet 路径；缺省与源文件同名")
    args = parser.parse_args(argv)

    from .core.dataset import (
        DatasetError,
        dataset_backend,
        dataset_path,
        load_records,
        load_s3_records,
        summarize,
        to_parquet,
    )

    # 没传路径且后端为 s3 时，从对象存储读；传了路径就按本地文件处理
    use_s3 = not args.path and dataset_backend() == "s3"
    source = Path(args.path).expanduser() if args.path else dataset_path()
    if not use_s3 and source is None:
        print(
            "[错误] 未指定数据集：传路径，或设置 SCRAPE_MCP_DATASET=<path.jsonl> / "
            "SCRAPE_MCP_DATASET_BACKEND=s3",
            file=sys.stderr,
        )
        return 2
    try:
        if args.action == "stats":
            records, bad = load_s3_records() if use_s3 else load_records(source)
            info = summarize(records)
            tools = " / ".join(f"{name} {count}" for name, count in info["by_tool"].items())
            print(f"[完成] {info['records']} 条记录（{tools or '空'}）")
            if info["first_ts"]:
                print(f"  时间：{info['first_ts']} → {info['last_ts']}")
            print(f"  平均 token {info['avg_tokens']} / 字符 {info['avg_chars']}")
            if bad:
                print(f"  跳过 {bad} 行损坏记录")
            return 0
        target = Path(args.out).expanduser() if args.out else None
        if use_s3:
            if target is None:
                print("[错误] S3 数据集导出 Parquet 需要 -o/--out 指定输出路径", file=sys.stderr)
                return 2
            records, _bad = load_s3_records()
            with tempfile.TemporaryDirectory() as tmp:
                staged = Path(tmp) / "dataset.jsonl"
                with staged.open("w", encoding="utf-8") as handle:
                    for item in records:
                        handle.write(json.dumps(item, ensure_ascii=False) + "\n")
                out = to_parquet(staged, target)
            print(f"[完成] {len(records)} 条记录 → {out}")
            return 0
        out = to_parquet(source, target)
        records, _bad = load_records(source)
        print(f"[完成] {len(records)} 条记录 → {out}")
        return 0
    except DatasetError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1


def _format_time(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "-"
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")


def _schedule_line(task: dict[str, Any]) -> str:
    selector = f"  [selector={task['selector']}]" if task.get("selector") else ""
    state = "启用" if task.get("enabled") else "暂停"
    return (
        f"{task.get('id')}  {task.get('kind', ''):<5} {state}  每 {task.get('interval_s')}s  "
        f"下次 {_format_time(task.get('next_run_at'))}  上次 {_format_time(task.get('last_run_at'))} "
        f"{task.get('last_status') or '-'}  {task.get('url')}{selector}"
    )


def _print_run(task: dict[str, Any], outcome: dict[str, Any]) -> int:
    if outcome.get("first_seen"):
        state = "建立基线"
    elif outcome.get("changed"):
        state = "检测到变更"
    elif outcome.get("ok"):
        state = "无变化"
    else:
        state = "失败"
    print(f"[执行] {task.get('id')} {task.get('url')}：{state}")
    if outcome.get("error"):
        print(f"  错误：{outcome['error']}")
    hook = outcome.get("webhook")
    if hook is not None:
        detail = hook.get("status") if hook.get("ok") else hook.get("error")
        print(f"  webhook：{'成功' if hook.get('ok') else '失败'}（{detail}）")
    return 0 if outcome.get("ok") else 1


def _cmd_schedule(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="scrape-mcp schedule",
        description="管理定时监控任务；daemon 在本机按间隔执行 web_watch / web_index。",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list", help="列出任务（ID、类型、状态、间隔、下次执行）")

    adder = sub.add_parser("add", help="新增或更新任务（同类型+URL+选择器只有一条）")
    adder.add_argument("--kind", choices=("watch", "index"), default="watch")
    adder.add_argument("--url", required=True, help="目标网页或站点 URL")
    adder.add_argument("--selector", help="只盯页面内某个区块（仅 watch）")
    adder.add_argument("--interval", type=int, default=3600, help="执行间隔秒，最小 60")
    adder.add_argument("--webhook", help="变更后 POST 的 http/https 地址")
    adder.add_argument("--session", default="default", help="执行时使用的登录态会话")
    adder.add_argument("--paused", action="store_true", help="创建后先暂停")

    for name, help_text in (
        ("pause", "暂停任务：保留状态，daemon 不再执行"),
        ("resume", "恢复任务：下一轮 daemon 立即执行"),
        ("delete", "删除任务与调度状态（页面快照不受影响）"),
        ("run", "立即执行一次，不等待下个周期"),
    ):
        runner = sub.add_parser(name, help=help_text)
        runner.add_argument("id", help="任务 ID（schedule list 可查）")
        if name == "run":
            runner.add_argument("--proxy", help="执行时使用的代理")

    daemon = sub.add_parser("daemon", help="按间隔执行到期任务（Ctrl+C 退出）")
    daemon.add_argument("--interval", type=int, default=30, help="扫描间隔秒，默认 30")
    daemon.add_argument("--once", action="store_true", help="只执行当前到期任务后退出")
    daemon.add_argument("--proxy", help="执行时使用的代理")
    args = parser.parse_args(argv)

    from .config import get_settings
    from .core.schedule import ScheduleError, ScheduleStore

    store = ScheduleStore(get_settings().data_dir)
    if args.action == "list":
        tasks = store.list()
        if not tasks:
            print("[空] 还没有定时任务；用 scrape-mcp schedule add --url <URL> 添加")
            return 0
        for task in tasks:
            print(_schedule_line(task))
        print(f"[完成] 共 {len(tasks)} 个任务，到期 {len(store.due())} 个")
        return 0

    if args.action == "add":
        try:
            task = store.create(
                kind=args.kind,
                url=args.url,
                selector=args.selector,
                interval_s=args.interval,
                webhook_url=args.webhook,
                session=args.session,
                enabled=False if args.paused else None,
            )
        except ScheduleError as exc:
            print(f"[错误] {exc}", file=sys.stderr)
            return 2
        print(f"[完成] 任务 {task['id']}（{task['kind']}）已保存：{task['url']}")
        if task["enabled"]:
            print("  下次执行：daemon 启动后的第一轮；也可先跑一次 schedule run <ID>")
        return 0

    if args.action in ("pause", "resume"):
        try:
            task = store.set_enabled(args.id, args.action == "resume")
        except ScheduleError as exc:
            print(f"[错误] {exc}", file=sys.stderr)
            return 2
        print(f"[完成] {args.id} 已{'恢复' if args.action == 'resume' else '暂停'}")
        if args.action == "resume":
            print(f"  下次执行：{_format_time(task.get('next_run_at'))}")
        return 0

    if args.action == "delete":
        if not store.delete(args.id):
            print(f"[错误] 任务不存在: {args.id}", file=sys.stderr)
            return 1
        print(f"[完成] 已删除 {args.id}")
        return 0

    # run / daemon 需要真正抓取，复用 Server 侧的执行逻辑，避免两套行为分叉
    from . import server as server_module

    if args.action == "run":
        task = store.get(args.id)
        if task is None:
            print(f"[错误] 任务不存在: {args.id}", file=sys.stderr)
            return 1
        outcome = asyncio.run(server_module.run_scheduled_task(task, proxy=args.proxy))
        return _print_run(task, outcome)

    async def _loop() -> int:
        while True:
            due = store.due()
            for task in due:
                outcome = await server_module.run_scheduled_task(task, proxy=args.proxy)
                _print_run(task, outcome)
            if args.once:
                if not due:
                    print("[完成] 没有到期任务")
                return 0
            await asyncio.sleep(max(1, args.interval))

    try:
        return asyncio.run(_loop())
    except KeyboardInterrupt:
        print("\n[停止] daemon 已退出")
        return 0


def run(argv: list[str]) -> int:
    """执行子命令；未知子命令给出用法提示。"""
    usage = "用法: scrape-mcp <init|schema|session|recipe|dataset|schedule> ..."
    if not argv:
        print(usage, file=sys.stderr)
        return 2
    command, rest = argv[0], argv[1:]
    if command == "init":
        return _cmd_init(rest)
    if command == "schema":
        return _cmd_schema(rest)
    if command == "session":
        return _cmd_session(rest)
    if command == "recipe":
        return _cmd_recipe(rest)
    if command == "dataset":
        return _cmd_dataset(rest)
    if command == "schedule":
        return _cmd_schedule(rest)
    print(f"未知子命令: {command}\n{usage}", file=sys.stderr)
    return 2
