"""子命令测试：init 生成/合并客户端配置，schema 导出工具定义，SDK 薄封装。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

import scrape_mcp
from scrape_mcp import cli, sdk, server
from scrape_mcp.config import Settings


def _run(argv: list[str]) -> int:
    return cli.run(argv)


def test_init_json_prints_stdout_only(capsys):
    code = _run(["init", "--client", "json", "--command", "/opt/bin/scrape-mcp"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    entry = out["mcpServers"]["scrape-mcp"]
    assert entry["command"] == "/opt/bin/scrape-mcp"
    assert entry["args"] == []
    assert "env" not in entry


def test_init_preview_does_not_create_file(tmp_path, capsys):
    target = tmp_path / "mcp.json"
    code = _run(["init", "--client", "cursor", "--path", str(target), "--command", "scrape-mcp"])
    assert code == 0
    assert not target.exists()
    assert "[预览]" in capsys.readouterr().out


def test_init_write_merges_existing_servers(tmp_path, capsys):
    target = tmp_path / "mcp.json"
    target.write_text(
        json.dumps({"mcpServers": {"other": {"command": "other-bin"}}}), encoding="utf-8"
    )

    code = _run(
        [
            "init",
            "--client",
            "cursor",
            "--path",
            str(target),
            "--command",
            "scrape-mcp",
            "--env",
            "SCRAPE_MCP_PROXY=http://127.0.0.1:10808",
            "--write",
        ]
    )
    assert code == 0
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["mcpServers"]["other"]["command"] == "other-bin"
    entry = data["mcpServers"]["scrape-mcp"]
    assert entry["command"] == "scrape-mcp"
    assert entry["env"] == {"SCRAPE_MCP_PROXY": "http://127.0.0.1:10808"}
    assert "[完成]" in capsys.readouterr().out


def test_init_existing_entry_needs_force(tmp_path, capsys):
    target = tmp_path / "mcp.json"
    target.write_text(
        json.dumps({"mcpServers": {"scrape-mcp": {"command": "old-bin", "args": []}}}),
        encoding="utf-8",
    )
    code = _run(["init", "--client", "cursor", "--path", str(target), "--command", "new-bin"])
    assert code == 1
    assert json.loads(target.read_text(encoding="utf-8"))["mcpServers"]["scrape-mcp"][
        "command"
    ] == ("old-bin")
    assert "--force" in capsys.readouterr().out


def test_init_force_overwrites_entry(tmp_path):
    target = tmp_path / "mcp.json"
    target.write_text(
        json.dumps({"mcpServers": {"scrape-mcp": {"command": "old-bin", "args": []}}}),
        encoding="utf-8",
    )
    code = _run(
        [
            "init",
            "--client",
            "cursor",
            "--path",
            str(target),
            "--command",
            "new-bin",
            "--write",
            "--force",
        ]
    )
    assert code == 0
    entry = json.loads(target.read_text(encoding="utf-8"))["mcpServers"]["scrape-mcp"]
    assert entry["command"] == "new-bin"


def test_init_rejects_broken_json_without_force(tmp_path, capsys):
    target = tmp_path / "mcp.json"
    target.write_text("{ not json", encoding="utf-8")
    code = _run(["init", "--client", "cursor", "--path", str(target), "--write"])
    assert code == 2
    assert "读取" in capsys.readouterr().err


def test_init_force_recovers_broken_json(tmp_path):
    target = tmp_path / "mcp.json"
    target.write_text("{ not json", encoding="utf-8")
    code = _run(
        [
            "init",
            "--client",
            "cursor",
            "--path",
            str(target),
            "--command",
            "x",
            "--write",
            "--force",
        ]
    )
    assert code == 0
    assert "scrape-mcp" in json.loads(target.read_text(encoding="utf-8"))["mcpServers"]


def test_init_rejects_bad_env(capsys):
    code = _run(["init", "--client", "json", "--env", "NO_EQUALS"])
    assert code == 2
    assert "KEY=VALUE" in capsys.readouterr().err


def test_init_vscode_uses_servers_key_and_stdio_type(tmp_path):
    target = tmp_path / "mcp.json"
    code = _run(
        ["init", "--client", "vscode", "--path", str(target), "--command", "scrape-mcp", "--write"]
    )
    assert code == 0
    out = json.loads(target.read_text(encoding="utf-8"))
    assert out["servers"]["scrape-mcp"]["type"] == "stdio"
    assert out["servers"]["scrape-mcp"]["command"] == "scrape-mcp"


def test_init_uses_installed_entry_when_command_missing(monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/usr/local/bin/scrape-mcp")
    _run(["init", "--client", "json"])
    entry = json.loads(capsys.readouterr().out)["mcpServers"]["scrape-mcp"]
    assert entry["command"] == "/usr/local/bin/scrape-mcp"
    assert entry["args"] == []


def test_init_falls_back_to_python_module(monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    _run(["init", "--client", "json"])
    entry = json.loads(capsys.readouterr().out)["mcpServers"]["scrape-mcp"]
    assert entry["args"] == ["-m", "scrape_mcp.server"]


def _fake_tool(name: str, **extra: Any) -> SimpleNamespace:
    base = {
        "name": name,
        "description": "说明",
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": None,
        "annotations": None,
    }
    base.update(extra)
    return SimpleNamespace(**base)


def test_schema_exports_all_tools(monkeypatch, capsys):
    async def fake_list() -> list[SimpleNamespace]:
        return [_fake_tool("web_fetch"), _fake_tool("web_extract")]

    monkeypatch.setattr(cli, "_list_tools", fake_list)
    code = _run(["schema"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["server"] == "scrape-mcp"
    assert payload["version"] == scrape_mcp.__version__
    assert [t["name"] for t in payload["tools"]] == ["web_fetch", "web_extract"]
    assert payload["tools"][0]["input_schema"]["type"] == "object"


def test_schema_filter_and_missing_tool(monkeypatch, capsys):
    async def fake_list() -> list[SimpleNamespace]:
        return [_fake_tool("web_fetch")]

    monkeypatch.setattr(cli, "_list_tools", fake_list)
    assert _run(["schema", "--tool", "web_fetch"]) == 0
    assert json.loads(capsys.readouterr().out)["tools"][0]["name"] == "web_fetch"
    assert _run(["schema", "--tool", "nope"]) == 2
    assert "nope" in capsys.readouterr().err


def test_schema_writes_output_file(monkeypatch, tmp_path):
    async def fake_list() -> list[SimpleNamespace]:
        return [
            _fake_tool("web_fetch", annotations=SimpleNamespace(model_dump=lambda **_: {"x": 1}))
        ]

    monkeypatch.setattr(cli, "_list_tools", fake_list)
    target = tmp_path / "tools.json"
    assert _run(["schema", "--output", str(target)]) == 0
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["tools"][0]["annotations"] == {"x": 1}


def test_session_cli_imports_netscape_and_lists(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "data"
    monkeypatch.setattr("scrape_mcp.config.get_settings", lambda: Settings(data_dir=str(data_dir)))
    source = tmp_path / "cookies.txt"
    source.write_text(
        "# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tFALSE\t0\tsid\tabc\n",
        encoding="utf-8",
    )

    code = _run(["session", "import", "--url", "https://www.example.com/a", "--file", str(source)])
    assert code == 0
    out = capsys.readouterr().out
    assert "1 条 cookie" in out and "www.example.com" in out
    assert (data_dir / "sessions" / "www.example.com.json").is_file()

    assert _run(["session", "list"]) == 0
    assert "www.example.com" in capsys.readouterr().out


def test_session_cli_rejects_bad_url_and_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("scrape_mcp.config.get_settings", lambda: Settings(data_dir=str(tmp_path)))
    bad = tmp_path / "cookies.txt"
    bad.write_text("nope", encoding="utf-8")

    assert _run(["session", "import", "--url", "www.example.com", "--file", str(bad)]) == 2
    assert "--url" in capsys.readouterr().err

    assert _run(["session", "import", "--url", "https://a.test", "--file", str(bad)]) == 2
    assert "无法解析" in capsys.readouterr().err


def test_unknown_subcommand_returns_usage(capsys):
    assert _run(["nope"]) == 2
    assert "未知子命令" in capsys.readouterr().err


def test_server_main_dispatches_subcommand(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(cli, "run", lambda argv: seen.append(argv) or 0)
    with pytest.raises(SystemExit) as exc:
        server.main(["init", "--client", "json"])
    assert exc.value.code == 0
    assert seen == [["init", "--client", "json"]]


async def test_sdk_wrappers_return_parsed_dict(monkeypatch):
    async def fake_fetch(url: str, **options: Any) -> str:
        return json.dumps({"ok": True, "url": url, "options": options})

    async def fake_extract(url: str, schema: Any = None, **options: Any) -> str:
        return json.dumps({"ok": True, "url": url, "schema": schema})

    async def fake_crawl(url: str, **options: Any) -> str:
        return json.dumps({"ok": True, "url": url})

    async def fake_batch(urls: list[str], **options: Any) -> str:
        return json.dumps({"ok": True, "urls": urls})

    monkeypatch.setattr(server, "web_fetch", fake_fetch)
    monkeypatch.setattr(server, "web_extract", fake_extract)
    monkeypatch.setattr(server, "web_crawl", fake_crawl)
    monkeypatch.setattr(server, "web_batch", fake_batch)

    assert (await sdk.fetch("https://a.test", max_tokens=10))["options"] == {"max_tokens": 10}
    assert (await sdk.extract("https://a.test", {"fields": {}}))["schema"] == {"fields": {}}
    assert (await sdk.crawl("https://a.test", max_pages=2))["ok"] is True
    assert (await sdk.batch(["https://a.test"]))["urls"] == ["https://a.test"]


def test_sdk_configures_logging_once(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(sdk, "_logging_ready", False)
    monkeypatch.setattr(sdk, "get_settings", lambda: SimpleNamespace(log_level="ERROR"))
    monkeypatch.setattr(sdk, "configure", lambda level: calls.append(level))
    sdk._ensure_logging()
    sdk._ensure_logging()
    assert calls == ["ERROR"]


def test_sdk_aclose_releases_fetcher(monkeypatch):
    released: list[bool] = []

    class FakeFetcher:
        async def aclose(self) -> None:
            released.append(True)

    monkeypatch.setattr(server, "_fetcher", FakeFetcher())
    asyncio.run(sdk.aclose())
    assert released == [True]
    assert server._fetcher is None


def test_sdk_module_is_the_programmatic_entry():
    assert sdk.fetch is sdk.__dict__["fetch"]
    assert set(sdk.__all__) == {"aclose", "batch", "crawl", "extract", "fetch", "watch"}
    # 顶层保持只有版本号，避免与 scrape_mcp.extract 子包重名
    assert scrape_mcp.__dict__.get("fetch") is None
