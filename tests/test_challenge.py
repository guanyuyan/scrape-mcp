"""挑战页诊断：厂商/类型识别、sitekey 提取、脱敏与兜底行为。"""

from __future__ import annotations

from scrape_mcp.core.challenge import analyze


def test_cloudflare_interstitial():
    html = "<html><title>Just a moment...</title><div id='cf-chl-widget'></div></html>"

    info = analyze(html, 403)

    assert info is not None
    assert info.provider == "cloudflare"
    assert info.kind == "interstitial"
    assert info.status == 403
    assert "cloudflare" in info.evidence or "cf-chl" in info.evidence


def test_turnstile_is_detected_before_cloudflare():
    html = '<html><body><div class="cf-turnstile" data-sitekey="0x4AAAAAAA123"></div></body></html>'

    info = analyze(html, 403)

    assert info is not None
    assert info.provider == "turnstile"
    assert info.kind == "captcha"
    assert info.sitekey == "0x4AAAAAAA123"


def test_recaptcha_extracts_sitekey_and_action():
    html = (
        '<html><body><div class="g-recaptcha" '
        'data-sitekey="6LcabcDEF_1234567890" data-action="login"></div></body></html>'
    )

    info = analyze(html, 403)

    assert info is not None
    assert info.provider == "recaptcha"
    assert info.kind == "captcha"
    assert info.sitekey == "6LcabcDEF_1234567890"
    assert info.action == "login"


def test_hcaptcha_page():
    html = '<html><body><div class="h-captcha" data-sitekey="10000000-ffff-ffff"></div></html>'

    info = analyze(html, 403)

    assert info is not None
    assert info.provider == "hcaptcha"
    assert info.kind == "captcha"
    assert info.sitekey == "10000000-ffff-ffff"


def test_datadome_js_challenge():
    html = "<html><script src='https://js.datadome.co/tags.js'></script></html>"

    info = analyze(html, 403)

    assert info is not None
    assert info.provider == "datadome"
    assert info.kind == "js_challenge"


def test_akamai_and_perimeterx_are_recognized():
    akamai = analyze("<html><script>var _abck = 'x';</script></html>", 403)
    px = analyze("<html><div id='px-captcha'></div></html>", 403)

    assert akamai is not None and akamai.provider == "akamai"
    assert px is not None and px.provider == "perimeterx"


def test_rate_limit_keeps_retry_after():
    info = analyze("<html><body>slow down</body></html>", 429, retry_after=3.5)

    assert info is not None
    assert info.provider == "unknown"
    assert info.kind == "rate_limit"
    assert info.retry_after == 3.5


def test_login_wall_on_401():
    html = '<html><body><form><input type="password" name="pwd"></form></body></html>'

    info = analyze(html, 401)

    assert info is not None
    assert info.kind == "login_wall"
    assert info.provider == "unknown"


def test_plain_error_page_is_not_a_challenge():
    assert analyze("<html><body>403 Forbidden</body></html>", 403) is None
    assert analyze("", 403) is None
    assert analyze("<html><body>ok</body></html>", 200) is None


def test_headers_feed_evidence():
    info = analyze(
        "<html><body>blocked</body></html>",
        429,
        {"Server": "cloudflare", "CF-RAY": "8a1b2c3d"},
    )

    assert info is not None
    assert "CF-RAY=8a1b2c3d" in info.evidence


def test_only_first_200kb_is_scanned():
    html = "x" * 200_001 + "Just a moment..."

    assert analyze(html, 403) is None


def test_as_dict_masks_sitekey():
    info = analyze('<html><div class="g-recaptcha" data-sitekey="abcdef123456"></div></html>', 403)

    assert info is not None
    full = info.as_dict()
    masked = info.as_dict(mask_sitekey=True)
    assert full["sitekey"] == "abcdef123456"
    assert masked["sitekey"] == "abcdef"
    assert masked["provider"] == "recaptcha"
    assert masked["kind"] == "captcha"
