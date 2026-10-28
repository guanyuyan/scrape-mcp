"""拦截判定测试。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scrape_mcp.core.detector import detect_block, is_terminal_rejection, looks_like_login_wall

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


def test_login_wall_needs_a_login_affordance():
    login_page = '<html><body><form><input name="user"><input type="password"></form></body></html>'
    assert looks_like_login_wall(login_page) is True
    assert looks_like_login_wall("<html><body><a>登录</a></body></html>") is True
    # 裸 403 拒绝页：没有密码框也没有登录字样，不该提示用户去登录
    bare_block = (
        "<html><head><title>Just a moment...</title></head><body>403 Forbidden</body></html>"
    )
    assert looks_like_login_wall(bare_block) is False


def test_terminal_rejection_only_for_bare_error_pages():
    bare_block = "<html><body><h1>403 Forbidden</h1></body></html>"
    assert is_terminal_rejection(403, bare_block) is True
    assert is_terminal_rejection(500, bare_block) is True
    # 正常页面即使状态码是 4xx 也不收工：还有挑战/放行的可能，交给原等待逻辑
    assert is_terminal_rejection(200, NORMAL) is False
    # Cloudflare 挑战常以 403 返回、几秒后自己放行，必须继续等
    assert is_terminal_rejection(403, CF) is False
    # 验证码、登录墙、待渲染的骨架页都要人/JS 参与，也不能提前收工
    assert is_terminal_rejection(403, CAPTCHA) is False
    assert is_terminal_rejection(403, '<html><body><input type="password"></body></html>') is False
    assert is_terminal_rejection(403, SPA) is False
    # 页面还没拿到任何内容时不下定论
    assert is_terminal_rejection(403, "  ") is False
