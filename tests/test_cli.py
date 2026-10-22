"""CLI 入口与打包元数据测试：防止入口、版本、类型标记在发布时失配。"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import scrape_mcp
from scrape_mcp import server

ROOT = Path(__file__).resolve().parents[1]


class _DummyMCP:
    """替身：记录 mcp.run 的调用参数，避免真正启动服务。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


def test_version_flag_prints_metadata_version(monkeypatch, capsys):
    import importlib.metadata

    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "9.9.9")
    server.main(["--version"])
    assert capsys.readouterr().out.strip() == "9.9.9"


def test_version_flag_falls_back_when_not_installed(monkeypatch, capsys):
    import importlib.metadata

    def _raise(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(importlib.metadata, "version", _raise)
    server.main(["--version"])
    assert "unknown" in capsys.readouterr().out


def test_help_flag_shows_usage_and_does_not_start(monkeypatch, capsys):
    dummy = _DummyMCP()
    monkeypatch.setattr(server, "mcp", dummy)
    server.main(["--help"])
    out = capsys.readouterr().out
    assert "用法: scrape-mcp" in out
    assert dummy.calls == []


def test_no_args_defaults_to_stdio(monkeypatch):
    dummy = _DummyMCP()
    monkeypatch.setattr(server, "mcp", dummy)
    server.main([])
    assert dummy.calls == [{"transport": "stdio"}]


def _http_settings(**overrides: object) -> SimpleNamespace:
    values = {
        "http_port": 8000,
        "http_host": "127.0.0.1",
        "http_json": False,
        "http_stateless": True,
        "http_token": None,
        "log_level": "WARNING",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_http_transport_port_override(monkeypatch):
    stub = _http_settings()
    monkeypatch.setattr(server, "get_settings", lambda: stub)
    calls: list[tuple[object, int, str | None]] = []
    monkeypatch.setattr(
        server, "_run_http", lambda settings, port, token: calls.append((settings, port, token))
    )
    server.main(["--transport", "streamable-http", "--port", "8123"])

    assert calls == [(stub, 8123, None)]


def test_http_public_bind_without_token_is_rejected(monkeypatch):
    stub = _http_settings(http_host="0.0.0.0")
    monkeypatch.setattr(server, "get_settings", lambda: stub)
    monkeypatch.setattr(server, "_run_http", lambda *args: pytest.fail("不应启动服务"))

    with pytest.raises(SystemExit) as exc:
        server.main(["--transport", "streamable-http"])

    assert "SCRAPE_MCP_HTTP_TOKEN" in str(exc.value)


def test_http_public_bind_passes_trimmed_token(monkeypatch):
    stub = _http_settings(http_host="0.0.0.0", http_token=" s3cret ")
    monkeypatch.setattr(server, "get_settings", lambda: stub)
    calls: list[tuple[object, int, str | None]] = []
    monkeypatch.setattr(
        server, "_run_http", lambda settings, port, token: calls.append((settings, port, token))
    )
    server.main(["--transport", "streamable-http"])

    assert calls == [(stub, 8000, "s3cret")]


def test_entry_point_declared_in_pyproject():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'scrape-mcp = "scrape_mcp.server:main"' in text


def test_version_consistent_between_package_and_pyproject():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, "pyproject.toml 缺少 version"
    assert match.group(1) == scrape_mcp.__version__


def test_py_typed_marker_present():
    assert (ROOT / "src" / "scrape_mcp" / "py.typed").is_file()


def test_wheel_declares_package_and_py_typed():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'packages = ["src/scrape_mcp"]' in text
    assert '"src/scrape_mcp/py.typed" = "scrape_mcp/py.typed"' in text
