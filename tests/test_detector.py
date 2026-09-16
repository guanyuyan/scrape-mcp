"""拦截判定测试。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scrape_mcp.core.detector import detect_block

NORMAL = "<html><body><article>" + "<p>正常正文内容。</p>" * 40 + "</article></body></html>"
CF = "<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>"
SPA = '<html><body><div id="app"></div><script src="a.js"></script></body></html>'
CAPTCHA = "<html><body><div>请完成安全验证</div></body></html>"


def test_normal_page():
    assert detect_block(200, NORMAL)[0] == "none"


def test_cloudflare():
    assert detect_block(503, CF)[0] == "cloudflare"
    assert detect_block(200, CF)[0] == "cloudflare"


def test_status_codes():
    assert detect_block(429, NORMAL)[0] == "rate_limit"
    assert detect_block(403, NORMAL)[0] == "waf"
    assert detect_block(500, NORMAL)[0] == "waf"


def test_captcha():
    assert detect_block(200, CAPTCHA)[0] == "captcha"


def test_captcha_word_in_long_article_is_not_block():
    article = (
        "<html><body><article><h1>验证码原理</h1>"
        + "<p>本文讲解验证码的实现。</p>" * 400
        + "</article></body></html>"
    )
    assert detect_block(200, article)[0] == "none"


def test_empty_and_spa():
    assert detect_block(200, SPA)[0] == "empty_spa"
    assert detect_block(200, "<html><body></body></html>")[0] == "empty"
