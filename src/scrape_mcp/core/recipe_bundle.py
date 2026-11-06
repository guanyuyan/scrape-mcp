"""recipe bundle：把本地 recipe 打包成可分享的单文件，也能导回来。

单份 recipe 一个 JSON 适合本机使用，但分享时要跨多个文件。bundle 把多份 recipe
收进一个对象，带 schema 版本与来源信息；导入前先整体校验，坏数据不会写进目录。

示例::

    {
      "schema": "mcp-scrape/recipes",
      "version": 1,
      "exported_at": "2026-11-06T05:00:00Z",
      "generator": "mcp-scrape 0.29.0",
      "recipes": [
        {"host": "example.com", "root_selector": "main article", "note": "示例"}
      ]
    }
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import __version__
from .recipes import RecipeIssue, recipe_host, validate_recipe_data

BUNDLE_SCHEMA = "mcp-scrape/recipes"
BUNDLE_VERSION = 1

_BUNDLE_KEYS = ("schema", "version", "exported_at", "generator", "recipes")


class RecipeBundleError(ValueError):
    """bundle 结构或内容不可用；``issues`` 里是逐条原因。"""

    def __init__(self, issues: list[RecipeIssue]) -> None:
        self.issues = issues
        super().__init__("; ".join(str(issue) for issue in issues))


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_bundle(
    directory: Path,
    hosts: Sequence[str] | None = None,
    exported_at: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """读目录里的 recipe 组成 bundle；返回 (bundle, 警告列表)，坏文件只记警告不中断。"""
    warnings: list[str] = []
    wanted = {host.strip().lower().lstrip(".") for host in hosts or ()}
    recipes: list[dict[str, Any]] = []
    found: set[str] = set()

    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            warnings.append(f"跳过 {path.name}：读取或解析失败（{exc}）")
            continue
        if not isinstance(data, dict):
            warnings.append(f"跳过 {path.name}：顶层不是 JSON 对象")
            continue
        issues = validate_recipe_data(data, path.stem)
        errors = [issue for issue in issues if issue.level == "error"]
        if errors:
            warnings.append(f"跳过 {path.name}：{'；'.join(str(item) for item in errors)}")
            continue
        warnings.extend(f"{path.name}：{item}" for item in issues)

        host = recipe_host(data, path.stem)
        found.add(host)
        if wanted and host not in wanted:
            continue
        payload = dict(data)
        payload["host"] = host
        recipes.append(payload)

    if wanted:
        for host in sorted(wanted - found):
            warnings.append(f"未找到 host 为 {host} 的 recipe")

    recipes.sort(key=lambda item: item["host"])
    bundle = {
        "schema": BUNDLE_SCHEMA,
        "version": BUNDLE_VERSION,
        "exported_at": exported_at or _now_iso(),
        "generator": f"mcp-scrape {__version__}",
        "recipes": recipes,
    }
    return bundle, warnings


def validate_bundle(data: Any) -> list[RecipeIssue]:
    """整体校验 bundle；返回问题列表，空列表代表可以安全导入。"""
    issues: list[RecipeIssue] = []
    if not isinstance(data, dict):
        return [RecipeIssue("error", "bundle 顶层必须是 JSON 对象")]

    for key in data:
        if key not in _BUNDLE_KEYS:
            issues.append(RecipeIssue("warning", f"bundle 里的未知字段 {key}，导入时忽略"))

    schema = data.get("schema")
    if schema != BUNDLE_SCHEMA:
        issues.append(RecipeIssue("error", f"schema 应为 {BUNDLE_SCHEMA!r}，收到 {schema!r}"))

    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        issues.append(RecipeIssue("error", f"version 必须是正整数，收到 {version!r}"))
    elif version > BUNDLE_VERSION:
        issues.append(
            RecipeIssue(
                "error",
                f"bundle 版本 {version} 高于当前支持的 {BUNDLE_VERSION}；请升级 mcp-scrape",
            )
        )

    entries = data.get("recipes")
    if not isinstance(entries, list):
        issues.append(RecipeIssue("error", "recipes 必须是数组"))
        return issues
    if not entries:
        issues.append(RecipeIssue("warning", "recipes 为空，导入不会写入任何文件"))
        return issues

    seen: dict[str, int] = {}
    for index, entry in enumerate(entries):
        label = f"recipes[{index}]"
        if not isinstance(entry, dict):
            issues.append(RecipeIssue("error", f"{label} 必须是 JSON 对象"))
            continue
        host = entry.get("host")
        if not isinstance(host, str) or not host.strip():
            issues.append(RecipeIssue("error", f"{label} 缺少 host（bundle 里必须显式给出）"))
            continue
        normalized = host.strip().lower().lstrip(".")
        for item in validate_recipe_data(entry, normalized):
            issues.append(RecipeIssue(item.level, f"{label}（{normalized}）：{item.message}"))
        if normalized in seen:
            issues.append(
                RecipeIssue(
                    "error",
                    f"{label} 的 host {normalized} 与 recipes[{seen[normalized]}] 重复",
                )
            )
            continue
        seen[normalized] = index
    return issues


@dataclass
class BundleImportResult:
    """导入结果；``dry_run`` 时 written 是「将会写入」的路径。"""

    written: list[Path] = field(default_factory=list)
    identical: list[Path] = field(default_factory=list)
    conflicts: list[Path] = field(default_factory=list)
    dry_run: bool = False


def _read_existing(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def import_bundle(
    data: Any,
    directory: Path,
    overwrite: bool = False,
    dry_run: bool = False,
) -> BundleImportResult:
    """校验并导入 bundle；校验失败抛 ``RecipeBundleError``，且不写入任何文件。"""
    issues = validate_bundle(data)
    if any(issue.level == "error" for issue in issues):
        raise RecipeBundleError([issue for issue in issues if issue.level == "error"])

    result = BundleImportResult(dry_run=dry_run)
    entries = data.get("recipes") if isinstance(data, dict) else None
    directory.mkdir(parents=True, exist_ok=True)
    for entry in entries or []:
        host = recipe_host(entry, "")
        target = directory / f"{host}.json"
        payload = dict(entry)
        payload["host"] = host
        if target.exists():
            existing = _read_existing(target)
            if existing == payload:
                result.identical.append(target)
                continue
            if not overwrite:
                result.conflicts.append(target)
                continue
        if not dry_run:
            target.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        result.written.append(target)
    return result
