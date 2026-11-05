"""数据集导出：JSONL 累积、概况、Parquet 转换与工具层接线。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from scrape_mcp import cli, server
from scrape_mcp.core import dataset
from scrape_mcp.core.dataset import DatasetError, build_record, load_records, summarize, to_parquet
from scrape_mcp.core.fetcher import FetchOutcome

URL = "https://a.test/x"
HTML = (
    "<html><head><title>示例标题</title></head>"
    "<body><article><h1>示例标题</h1><p>正文内容，用于数据集测试。</p></article></body></html>"
)


def _settings(path) -> SimpleNamespace:
    return SimpleNamespace(dataset=str(path) if path else "")


def _payload(**overrides) -> dict:
    base = {
        "ok": True,
        "url": URL,
        "final_url": URL,
        "status": 200,
        "tier": "l1",
        "title": "示例标题",
        "content": "正文" * 10,
        "token_estimate": 20,
        "error_code": None,
    }
    base.update(overrides)
    return base


def test_record_result_appends_jsonl(tmp_path, monkeypatch):
    path = tmp_path / "nested" / "ds.jsonl"
    monkeypatch.setattr(dataset, "get_settings", lambda: _settings(path))

    dataset.record_result(_payload(), tool="fetch")
    dataset.record_result(_payload(content=["块一", "块二"]), tool="extract")

    records, bad = load_records(path)
    assert bad == 0
    assert [r["tool"] for r in records] == ["fetch", "extract"]
    first, second = records
    assert first["v"] == dataset.RECORD_VERSION
    assert first["url"] == URL
    assert first["chars"] == 20
    assert first["chars"] == len("正文" * 10)
    assert second["chars"] == 4  # 块列表按总字符数记
    assert first["ts"].endswith("+00:00")


def test_record_skips_when_disabled_or_failed(tmp_path, monkeypatch):
    path = tmp_path / "ds.jsonl"
    monkeypatch.setattr(dataset, "get_settings", lambda: _settings(""))
    dataset.record_result(_payload(), tool="fetch")
    assert not path.exists()

    monkeypatch.setattr(dataset, "get_settings", lambda: _settings(path))
    dataset.record_result(_payload(ok=False, error_code="blocked"), tool="fetch")
    assert not path.exists()


def test_record_write_failure_does_not_raise(tmp_path, monkeypatch):
    # 指向一个"文件当目录用"的路径，触发 OSError；导出是旁路，不该抛出
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(dataset, "get_settings", lambda: _settings(blocker / "ds.jsonl"))
    dataset.record_result(_payload(), tool="fetch")


class _MemStore:
    """内存版 S3 对象存储，够测试 dataset 的写读路径。"""

    def __init__(self) -> None:
        self.items: dict[str, dict] = {}

    def put_json(self, key: str, data: dict) -> None:
        self.items[key] = data

    def get_json(self, key: str) -> dict | None:
        return self.items.get(key)

    def list_keys(self) -> list[str]:
        return sorted(self.items)


def test_record_result_writes_one_object_per_record(monkeypatch):
    store = _MemStore()
    settings = SimpleNamespace(dataset="", dataset_backend="s3")
    monkeypatch.setattr(dataset, "get_settings", lambda: settings)
    monkeypatch.setattr(dataset, "build_s3_object_store", lambda namespace: store)

    dataset.record_result(_payload(), tool="fetch")
    dataset.record_result(_payload(url="https://b.test/y"), tool="extract")

    keys = list(store.items)
    assert len(keys) == 2
    assert all(key.endswith(".json") and key.count("/") == 1 for key in keys)
    assert sorted(item["tool"] for item in store.items.values()) == ["extract", "fetch"]


def test_record_result_s3_skips_failed_payload(monkeypatch):
    store = _MemStore()
    settings = SimpleNamespace(dataset="", dataset_backend="s3")
    monkeypatch.setattr(dataset, "get_settings", lambda: settings)
    monkeypatch.setattr(dataset, "build_s3_object_store", lambda namespace: store)

    dataset.record_result(_payload(ok=False, error_code="blocked"), tool="fetch")

    assert store.items == {}


def test_load_s3_records_sorts_and_counts_broken(monkeypatch):
    store = _MemStore()
    store.items = {
        "2026-11-05/a.json": {"tool": "fetch", "ts": "2026-11-05T01:00:00+00:00"},
        "2026-11-05/b.json": {"tool": "extract", "ts": "2026-11-05T00:00:00+00:00"},
    }

    class BrokenStore(_MemStore):
        def get_json(self, key: str) -> dict | None:
            return None if key.endswith("c.json") else self.items[key]

        def list_keys(self) -> list[str]:
            return ["2026-11-05/a.json", "2026-11-05/b.json", "2026-11-05/c.json"]

    broken = BrokenStore()
    broken.items = store.items
    monkeypatch.setattr(dataset, "build_s3_object_store", lambda namespace: broken)
    records, bad = dataset.load_s3_records()

    assert [r["tool"] for r in records] == ["extract", "fetch"]
    assert bad == 1


def test_build_record_carries_extract_data():
    record = build_record(_payload(data={"title": "t"}), tool="extract")
    assert record["data"] == {"title": "t"}
    assert build_record(_payload(), tool="fetch").get("data") is None


def test_load_skips_broken_lines(tmp_path):
    path = tmp_path / "ds.jsonl"
    good = json.dumps(
        {"v": 1, "tool": "fetch", "chars": 10, "tokens": 2, "ts": "2026-10-29T00:00:00+00:00"}
    )
    path.write_text(f"{good}\nnot json\n\n[1,2]\n", encoding="utf-8")
    records, bad = load_records(path)
    assert len(records) == 1
    assert bad == 2


def test_load_missing_file_raises(tmp_path):
    with pytest.raises(DatasetError):
        load_records(tmp_path / "nope.jsonl")


def test_summarize_counts_and_averages():
    records = [
        {"tool": "fetch", "chars": 10, "tokens": 5, "ts": "2026-10-29T00:00:00+00:00"},
        {"tool": "fetch", "chars": 30, "tokens": 15, "ts": "2026-10-29T01:00:00+00:00"},
        {"tool": "crawl", "chars": 20, "tokens": 10},
    ]
    info = summarize(records)
    assert info["records"] == 3
    assert info["by_tool"] == {"crawl": 1, "fetch": 2}
    assert info["avg_chars"] == 20
    assert info["avg_tokens"] == 10
    assert info["first_ts"].startswith("2026-10-29T00")
    assert info["last_ts"].startswith("2026-10-29T01")


def test_to_parquet_without_pyarrow_explains(tmp_path, monkeypatch):
    path = tmp_path / "ds.jsonl"
    path.write_text(json.dumps({"url": URL}) + "\n", encoding="utf-8")
    monkeypatch.setattr(dataset.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(DatasetError) as excinfo:
        to_parquet(path)
    assert "mcp-scrape[dataset]" in str(excinfo.value)


def test_cli_dataset_stats(tmp_path, capsys):
    path = tmp_path / "ds.jsonl"
    rows = [
        {"v": 1, "ts": "2026-10-29T00:00:00+00:00", "tool": "fetch", "tokens": 10, "chars": 100},
        {"v": 1, "ts": "2026-10-29T01:00:00+00:00", "tool": "extract", "tokens": 30, "chars": 300},
    ]
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )

    assert cli.run(["dataset", "stats", str(path)]) == 0
    out = capsys.readouterr().out
    assert "2 条记录" in out
    assert "fetch 1" in out and "extract 1" in out
    assert "平均 token 20 / 字符 200" in out


def test_cli_dataset_requires_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dataset, "dataset_path", lambda: None)
    assert cli.run(["dataset", "stats"]) == 2
    assert "SCRAPE_MCP_DATASET" in capsys.readouterr().err


def test_web_fetch_writes_dataset(tmp_path, monkeypatch):
    path = tmp_path / "ds.jsonl"
    monkeypatch.setattr(dataset, "get_settings", lambda: _settings(path))

    outcome = FetchOutcome(
        url=URL,
        final_url=URL,
        status=200,
        tier="l1",
        blocked="none",
        block_reason="",
        content_type="text/html",
        html=HTML,
        elapsed_ms=8,
    )

    class _FakeFetcher:
        async def fetch(self, url: str, **_kwargs) -> FetchOutcome:
            return outcome

        async def aclose(self) -> None:
            pass

    async def allowed(_url: str, *_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(server, "_compliance_check", allowed)
    monkeypatch.setattr(server, "_get_fetcher", lambda: _FakeFetcher())

    out = json.loads(asyncio.run(server.web_fetch(URL)))
    assert out["ok"] is True
    records, _bad = load_records(path)
    assert len(records) == 1
    assert records[0]["tool"] == "fetch"
    assert records[0]["title"] == "示例标题"
    assert "正文内容" in records[0]["content"]
