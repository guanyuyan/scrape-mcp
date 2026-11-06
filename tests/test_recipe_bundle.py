"""recipe bundle：导出打包、整体校验与导入落盘。"""

from __future__ import annotations

import json

import pytest

from scrape_mcp.core.recipe_bundle import (
    BUNDLE_SCHEMA,
    BUNDLE_VERSION,
    RecipeBundleError,
    build_bundle,
    import_bundle,
    validate_bundle,
)
from scrape_mcp.core.recipes import RecipeStore


def _write(directory, name: str, data) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    (directory / name).write_text(text, encoding="utf-8")


def _bundle(recipes, **extra) -> dict:
    payload = {
        "schema": BUNDLE_SCHEMA,
        "version": BUNDLE_VERSION,
        "exported_at": "2026-11-06T00:00:00Z",
        "generator": "mcp-scrape test",
        "recipes": recipes,
    }
    payload.update(extra)
    return payload


# ---------- export ----------


def test_build_bundle_collects_metadata_and_sorts(tmp_path):
    _write(tmp_path, "b.com.json", {"root_selector": "main"})
    _write(tmp_path, "a.com.json", {"host": "a.com", "wait_for": ".x", "note": "站点 A"})

    bundle, warnings = build_bundle(tmp_path, exported_at="2026-11-06T00:00:00Z")

    assert bundle["schema"] == BUNDLE_SCHEMA
    assert bundle["version"] == BUNDLE_VERSION
    assert bundle["exported_at"] == "2026-11-06T00:00:00Z"
    assert bundle["generator"].startswith("mcp-scrape ")
    assert [item["host"] for item in bundle["recipes"]] == ["a.com", "b.com"]
    assert warnings == []


def test_build_bundle_skips_broken_and_invalid_files(tmp_path):
    _write(tmp_path, "good.com.json", {"root_selector": "main"})
    _write(tmp_path, "broken.com.json", '{"host": ')
    _write(tmp_path, "list.com.json", [])
    _write(tmp_path, "bad.com.json", {"root_selector": "div["})

    bundle, warnings = build_bundle(tmp_path)

    assert [item["host"] for item in bundle["recipes"]] == ["good.com"]
    assert len(warnings) == 3
    assert all("跳过" in item for item in warnings)


def test_build_bundle_filters_hosts_and_reports_missing(tmp_path):
    _write(tmp_path, "a.com.json", {"root_selector": "main"})
    _write(tmp_path, "b.com.json", {"root_selector": "article"})

    bundle, warnings = build_bundle(tmp_path, ["a.com"])

    assert [item["host"] for item in bundle["recipes"]] == ["a.com"]
    assert warnings == []

    bundle, warnings = build_bundle(tmp_path, ["zzz.com"])
    assert bundle["recipes"] == []
    assert any("未找到 host" in item for item in warnings)


# ---------- validate ----------


def test_validate_bundle_accepts_good_payload():
    issues = validate_bundle(_bundle([{"host": "example.com", "root_selector": "main"}]))
    assert [issue for issue in issues if issue.level == "error"] == []


@pytest.mark.parametrize(
    "payload, expected",
    [
        ([], "顶层必须是 JSON 对象"),
        ({"schema": "other", "version": 1, "recipes": []}, "schema 应为"),
        ({"schema": BUNDLE_SCHEMA, "version": 99, "recipes": []}, "高于当前支持"),
        ({"schema": BUNDLE_SCHEMA, "version": 1, "recipes": {}}, "recipes 必须是数组"),
        (_bundle([{"root_selector": "main"}]), "缺少 host"),
        (_bundle([{"host": "a.com"}, {"host": "a.com"}]), "重复"),
        (_bundle([{"host": "a.com", "root_selector": "div["}]), "选择器语法有误"),
    ],
)
def test_validate_bundle_reports_errors(payload, expected):
    issues = validate_bundle(payload)
    errors = [issue for issue in issues if issue.level == "error"]
    assert errors and expected in errors[0].message


def test_validate_bundle_warns_on_unknown_keys_and_empty_recipes():
    issues = validate_bundle(_bundle([], extra="x"))
    messages = {issue.level: issue.message for issue in issues}
    assert any(issue.level == "warning" for issue in issues)
    assert "recipes 为空" in messages["warning"]


# ---------- import ----------


def test_import_roundtrip_into_empty_directory(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    _write(source, "a.com.json", {"root_selector": "main", "note": "A"})
    _write(source, "b.com.json", {"crawl": {"next_selector": "a.next", "max_pages": 3}})
    bundle, _warnings = build_bundle(source)

    result = import_bundle(bundle, target)

    assert sorted(path.name for path in result.written) == ["a.com.json", "b.com.json"]
    store = RecipeStore(target)
    assert store.for_url("https://a.com/x").note == "A"
    assert store.for_url("https://b.com/x").max_pages == 3


def test_import_requires_overwrite_for_conflicts(tmp_path):
    _write(tmp_path, "a.com.json", {"root_selector": "#old"})
    bundle = _bundle([{"host": "a.com", "root_selector": "#new"}])

    result = import_bundle(bundle, tmp_path)
    assert [path.name for path in result.conflicts] == ["a.com.json"]
    assert result.written == []
    assert RecipeStore(tmp_path).for_url("https://a.com/x").root_selector == "#old"

    result = import_bundle(bundle, tmp_path, overwrite=True)
    assert [path.name for path in result.written] == ["a.com.json"]
    assert RecipeStore(tmp_path).for_url("https://a.com/x").root_selector == "#new"


def test_import_identical_recipe_is_not_a_conflict(tmp_path):
    bundle = _bundle([{"host": "a.com", "root_selector": "#same"}])
    first = import_bundle(bundle, tmp_path)
    second = import_bundle(bundle, tmp_path)

    assert [path.name for path in first.written] == ["a.com.json"]
    assert [path.name for path in second.identical] == ["a.com.json"]
    assert second.written == []
    assert second.conflicts == []


def test_import_dry_run_writes_nothing(tmp_path):
    bundle = _bundle([{"host": "a.com", "root_selector": "main"}])
    result = import_bundle(bundle, tmp_path, dry_run=True)

    assert result.dry_run is True
    assert [path.name for path in result.written] == ["a.com.json"]
    assert not (tmp_path / "a.com.json").exists()


def test_import_rejects_bad_bundle_without_writing(tmp_path):
    bundle = _bundle(
        [{"host": "good.com", "root_selector": "main"}, {"host": "bad.com", "scroll": "yes"}]
    )

    with pytest.raises(RecipeBundleError) as excinfo:
        import_bundle(bundle, tmp_path)

    assert any("scroll" in issue.message for issue in excinfo.value.issues)
    assert not (tmp_path / "good.com.json").exists()


# ---------- CLI ----------


def test_cli_export_to_file_and_stdout(tmp_path, capsys):
    from scrape_mcp import cli

    source = tmp_path / "recipes"
    _write(source, "a.com.json", {"root_selector": "main"})
    out = tmp_path / "bundle.json"

    assert cli.run(["recipe", "export", "--dir", str(source), "-o", str(out)]) == 0
    assert "导出 1 个 recipe" in capsys.readouterr().out
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["schema"] == BUNDLE_SCHEMA

    assert cli.run(["recipe", "export", "--dir", str(source)]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["recipes"][0]["host"] == "a.com"


def test_cli_export_reports_missing_host(tmp_path, capsys):
    from scrape_mcp import cli

    source = tmp_path / "recipes"
    _write(source, "a.com.json", {"root_selector": "main"})

    assert cli.run(["recipe", "export", "--dir", str(source), "zzz.com"]) == 1
    assert "未找到 host" in capsys.readouterr().err


def test_cli_import_reports_conflict_and_overwrite(tmp_path, capsys):
    from scrape_mcp import cli

    bundle = tmp_path / "bundle.json"
    bundle.write_text(
        json.dumps(_bundle([{"host": "a.com", "root_selector": "#new"}])), encoding="utf-8"
    )
    target = tmp_path / "recipes"
    _write(target, "a.com.json", {"root_selector": "#old"})

    assert cli.run(["recipe", "import", str(bundle), "--dir", str(target)]) == 1
    assert "冲突" in capsys.readouterr().err

    assert cli.run(["recipe", "import", str(bundle), "--dir", str(target), "--overwrite"]) == 0
    assert "写入 1 个" in capsys.readouterr().out
    assert (
        json.loads((target / "a.com.json").read_text(encoding="utf-8"))["root_selector"] == "#new"
    )


def test_cli_import_rejects_bad_bundle(tmp_path, capsys):
    from scrape_mcp import cli

    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps({"recipes": []}), encoding="utf-8")

    assert cli.run(["recipe", "import", str(bundle), "--dir", str(tmp_path)]) == 1
    assert "bundle 不可用" in capsys.readouterr().err
