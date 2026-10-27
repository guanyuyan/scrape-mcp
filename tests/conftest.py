"""测试公共设施：隔离站点 recipe 与数据目录，避免读到本机状态或互相命中缓存。"""

from __future__ import annotations

import pytest

from scrape_mcp import metrics, server
from scrape_mcp.config import Settings
from scrape_mcp.core import stats as stats_module
from scrape_mcp.core.recipes import RecipeStore


@pytest.fixture(autouse=True)
def _isolated_recipes(tmp_path, monkeypatch):
    store = RecipeStore(tmp_path / "recipes")
    monkeypatch.setattr(server, "get_recipe_store", lambda: store)
    return store


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """数据目录指向 tmp_path，并默认关闭结果缓存（每个用例独立）。

    跨用例共享结果缓存会让"同一 URL 不同 patch 结果"互相污染，
    需要验证缓存的用例自行把 cache_ttl 打开。
    """
    settings = Settings(data_dir=str(tmp_path / "data"), cache_ttl=0)
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    return settings


@pytest.fixture(autouse=True)
def _fresh_stats():
    """每个用例从零开始计数，避免 /stats 断言受其他用例影响。"""
    return stats_module.reset_stats()


@pytest.fixture(autouse=True)
def _fresh_metrics():
    """每个用例从零开始计数，避免 /metrics 的拒绝计数受其他用例影响。"""
    metrics.reset()
