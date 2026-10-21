"""真实站点基准的离线单测：站点集校验、汇总口径与渲染，不联网。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("bench_sites", ROOT / "scripts" / "bench_sites.py")
bench = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(bench)


def _record(**overrides) -> dict:
    record = {
        "id": "demo",
        "url": "https://example.test/",
        "category": "docs",
        "expect": "ok",
        "ok": True,
        "matched": True,
        "passed": True,
        "tier": "l1",
        "kind": "html",
        "status": 200,
        "error_code": None,
        "error": None,
        "blocked": None,
        "chars": 1000,
        "tokens": 200,
        "html_bytes": 5000,
        "elapsed_ms": 100,
        "payload_ms": 100,
        "checks_total": 1,
        "checks_hit": 1,
        "missing_checks": [],
        "truncated": False,
    }
    record.update(overrides)
    return record


def test_sites_file_is_valid():
    sites = bench.load_sites(ROOT / "benchmarks" / "sites.json")
    assert len(sites) >= 20
    assert {s["category"] for s in sites} == set(bench.CATEGORY_LABELS)
    assert any(s.get("nightly") for s in sites)
    for site in sites:
        if site.get("expect", "ok") == "ok":
            assert site["min_chars"] > 0, site["id"]


def test_select_sites_filters():
    sites = bench.load_sites(ROOT / "benchmarks" / "sites.json")
    docs = bench.select_sites(sites, ["docs"], None, False)
    assert docs and all(s["category"] == "docs" for s in docs)

    nightly = bench.select_sites(sites, None, None, True)
    assert nightly and all(s.get("nightly") for s in nightly)

    one = bench.select_sites(sites, None, ["py-asyncio"], False)
    assert [s["id"] for s in one] == ["py-asyncio"]


def test_missing_checks_is_case_insensitive():
    assert bench._missing_checks("Hello World", ["hello", "world"]) == []
    assert bench._missing_checks("Hello", ["hello", "gone"]) == ["gone"]


def test_summarize_counts_passes_and_intercepts():
    records = [
        _record(id="a"),
        _record(id="b", chars=0, tokens=0, ok=False, matched=False, passed=False),
        _record(
            id="c",
            expect="login_required",
            category="anti_bot",
            ok=False,
            matched=True,
            passed=True,
            status=401,
            error_code="login_required",
            chars=0,
            tokens=0,
            elapsed_ms=300,
            checks_total=0,
            checks_hit=0,
            tier=None,
        ),
    ]
    summary = bench.summarize(records)
    assert summary["sites"] == 3
    assert summary["passed"] == 2
    assert summary["expected_ok"] == 2
    assert summary["fetch_ok"] == 1
    assert summary["expected_block"] == 1
    assert summary["intercepted"] == 1
    assert summary["intercept_rate"] == 1.0
    assert summary["check_rate"] == 1.0
    assert summary["tiers"] == {"l1": 1}
    assert summary["by_category"]["anti_bot"]["passed"] == 1


def test_render_markdown_has_summary_and_rows():
    records = [_record(id="a")]
    text = bench.render_markdown(records, bench.summarize(records))
    assert "真实站点质量基准" in text
    assert "| 站点 | 分类 | 层级 | 结果 |" in text
    assert "| a | docs | l1 | 通过" in text


def test_compare_baseline_flags_content_regression():
    baseline = {"records": [{"id": "a", "ok": True, "chars": 1000, "tokens": 200}]}
    rows = bench.compare_baseline([_record(id="a", chars=700, tokens=200)], baseline)
    assert len(rows) == 1
    assert rows[0]["regression"] is True
    assert rows[0]["char_delta"] == -0.3


def test_render_markdown_includes_reference_ratio():
    records = [_record(id="a", tokens=100)]
    text = bench.render_markdown(
        records, bench.summarize(records), {"a": {"chars": 900, "tokens": 200}}
    )
    assert "| a | 100 | 200 | 0.50× |" in text


def test_sites_json_is_utf8_json():
    data = json.loads((ROOT / "benchmarks" / "sites.json").read_text(encoding="utf-8"))
    assert data["version"] == 1


def test_baseline_matches_corpus_and_schema():
    baseline = json.loads((ROOT / "benchmarks" / "baseline.json").read_text(encoding="utf-8"))
    corpus_ids = {s["id"] for s in bench.load_sites(ROOT / "benchmarks" / "sites.json")}
    record_ids = {r["id"] for r in baseline["records"]}
    assert record_ids
    assert record_ids <= corpus_ids
    assert baseline["source"].startswith("nightly run")
    assert baseline["sites_file"] == "benchmarks/sites.json"
    assert baseline["summary"]["sites"] == len(baseline["records"])
    for record in baseline["records"]:
        assert {"chars", "tokens", "ok"} <= set(record)
