"""M5 隐身加固：_STEALTH_JS 模板可格式化、无残留占位符、含关键对抗。"""

from __future__ import annotations

from scrape_mcp.core import browser as br


def test_stealth_js_formats_without_leftover_placeholders():
    src = br._STEALTH_JS % {
        "languages": "['zh-CN','zh']",
        "cr": 10,
        "cg": 20,
        "cb": 30,
    }
    assert "%(" not in src and "%s" not in src
    assert "rgba(10,20,30,0.02)" in src


def test_stealth_js_contains_key_antifingerprint_bits():
    src = br._STEALTH_JS
    assert "webdriver" in src
    for probe_hint in ("cdc_", "__selenium", "userAgentData", "toDataURL", "hardwareConcurrency"):
        assert probe_hint in src
