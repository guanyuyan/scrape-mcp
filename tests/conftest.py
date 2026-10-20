"""测试公共设施：隔离站点 recipe，避免读到开发者本机的 ~/.scrape_mcp/recipes。"""

from __future__ import annotations

import pytest

from scrape_mcp import server
from scrape_mcp.core.recipes import RecipeStore


@pytest.fixture(autouse=True)
def _isolated_recipes(tmp_path, monkeypatch):
    store = RecipeStore(tmp_path / "recipes")
    monkeypatch.setattr(server, "get_recipe_store", lambda: store)
    return store
