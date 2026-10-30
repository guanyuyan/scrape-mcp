"""浏览器动作链：参数校验、逐步执行与失败记录。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from scrape_mcp import server
from scrape_mcp.config import Settings
from scrape_mcp.core.browser import BrowserOutcome, BrowserRenderer
from scrape_mcp.core.fetcher import Fetcher


def test_actions_arg_accepts_chain():
    actions, err = server._actions_arg(
        [
            {"action": "click", "selector": "#kw"},
            {"action": "type", "selector": "#kw", "text": "关键词"},
            {"action": "press", "key": "Enter"},
            {"action": "wait", "selector": "#result"},
            {"action": "select", "selector": "#city", "value": "北京"},
            {"action": "scroll", "times": 2},
        ]
    )
    assert err is None
    assert [item["action"] for item in actions] == [
        "click",
        "type",
        "press",
        "wait",
        "select",
        "scroll",
    ]
    assert actions[5]["times"] == 2


@pytest.mark.parametrize(
    "actions, message",
    [
        ("not-a-list", "对象数组"),
        ([{"action": "run"}], "action 需为"),
        ([{"action": "click"}], "必须提供 selector"),
        ([{"action": "type", "selector": "#kw"}], "text 必须是字符串"),
        ([{"action": "press"}], "必须提供 key"),
        ([{"action": "wait"}], "需要 selector 或"),
        ([{"action": "scroll", "times": 99}], "times 需为 1~20"),
    ],
)
def test_actions_arg_rejects_invalid(actions, message):
    _normalized, err = server._actions_arg(actions)
    assert err is not None
    assert err["error_code"] == "invalid_argument"
    assert message in err["error"]


def test_actions_arg_rejects_too_many():
    _normalized, err = server._actions_arg([{"action": "click", "selector": "#x"}] * 21)
    assert err is not None and "最多 20 个" in err["error"]


def test_web_fetch_rejects_invalid_actions_before_fetch():
    out = json.loads(
        asyncio.run(server.web_fetch("https://a.test/x", actions=[{"action": "exec"}]))
    )
    assert out["ok"] is False
    assert out["error_code"] == "invalid_argument"


class _FakePage:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.pressed: list[str] = []

    async def click(self, selector, timeout):
        self.calls.append(("click", selector))
        if selector == "#bad":
            raise RuntimeError("no such element")

    async def fill(self, selector, text, timeout):
        self.calls.append(("fill", selector, text))

    async def press(self, selector, key, timeout):
        self.calls.append(("press", selector, key))

    async def select_option(self, selector, value, timeout):
        self.calls.append(("select", selector, value))

    async def wait_for_selector(self, selector, timeout):
        self.calls.append(("wait", selector))

    async def wait_for_timeout(self, ms):
        self.calls.append(("sleep", ms))

    async def evaluate(self, script):
        self.calls.append(("evaluate", script))

    def locator(self, selector):
        page = self

        class _Locator:
            async def scroll_into_view_if_needed(self, timeout):
                page.calls.append(("locator", selector))

        return _Locator()

    async def keyboard_press(self, key):
        self.pressed.append(key)


def _renderer() -> BrowserRenderer:
    renderer = BrowserRenderer.__new__(BrowserRenderer)
    renderer._s = SimpleNamespace(l2_navigate_timeout=15)  # type: ignore[assignment]
    return renderer


def test_apply_actions_records_failures_and_continues():
    page = _FakePage()
    # keyboard.press 是同步属性访问，这里补一个异步对象
    page.keyboard = SimpleNamespace(press=page.keyboard_press)
    notes: list[str] = []
    trail = asyncio.run(
        _renderer()._apply_actions(
            page,
            [
                {"action": "click", "selector": "#ok"},
                {"action": "click", "selector": "#bad"},
                {"action": "press", "key": "Enter"},
                {"action": "scroll", "times": 1},
            ],
            notes,
            timeout=10,
        )
    )
    assert trail[0] == {"action": "click", "ok": True}
    assert trail[1]["ok"] is False and trail[1]["detail"] == "RuntimeError"
    assert trail[2] == {"action": "press", "ok": True}
    assert trail[3] == {"action": "scroll", "ok": True}
    assert notes and "action[2] click 失败" in notes[0]
    assert page.pressed == ["Enter"]


def test_fetcher_with_actions_uses_browser_only(tmp_path):
    settings = Settings(data_dir=str(tmp_path), l2_enabled=True)
    fetcher = Fetcher(settings)

    class _FakeBrowser:
        def __init__(self) -> None:
            self.kwargs: dict = {}

        async def render(self, url, host, **kwargs):
            self.kwargs = kwargs
            return BrowserOutcome(
                ok=True,
                url=url,
                final_url=url,
                status=200,
                html="<html><body>ok</body></html>",
                elapsed_ms=4,
                actions=[{"action": "click", "ok": True}],
            )

        def has_session(self, host, session="default"):
            return False

    fake = _FakeBrowser()
    fetcher._browser = fake  # type: ignore[assignment]
    actions = [{"action": "click", "selector": "#go"}]
    outcome = asyncio.run(fetcher.fetch("https://a.test/x", actions=actions))
    assert outcome.tier == "l2"
    assert fake.kwargs["actions"] == actions
    assert outcome.l2["actions"] == [{"action": "click", "ok": True}]
