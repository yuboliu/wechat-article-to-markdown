"""两级抓取层的单元测试：请求头、风控页判定、浏览器探测、降级调度。

这些函数都是纯逻辑，不需要网络与浏览器，所以放在默认 CI 路径里跑。
"""

import asyncio

import pytest

import wechat_article_to_markdown as wtm
from wechat_article_to_markdown import (
    MIN_ARTICLE_HTML_CHARS,
    _parse_cookie_header,
    build_headers,
    detect_page_problem,
    fetch_html,
    find_local_chromium,
)

URL = "https://mp.weixin.qq.com/s/abcdef"


def _article_html(extra: str = "") -> str:
    """造一个"看起来正常"的文章页：正文容器 + 标题元素 + 体量都齐备。"""
    return (
        '<html><head><title>t</title></head><body>'
        '<h1 id="activity-name">测试标题</h1>'
        f'<div id="js_content">{extra}</div>'
        + "正文" * (MIN_ARTICLE_HTML_CHARS // 2)
        + "</body></html>"
    )


# ------------------------------------------------------------------
# build_headers
# ------------------------------------------------------------------


def test_build_headers_defaults() -> None:
    headers = build_headers()

    assert headers["User-Agent"].startswith("Mozilla/5.0")
    assert headers["Referer"] == "https://mp.weixin.qq.com/"
    assert headers["Accept-Language"].startswith("zh-CN")
    # 故意不设 Accept-Encoding，交给 requests 按已装解码器协商
    assert "Accept-Encoding" not in headers
    assert "Cookie" not in headers


def test_build_headers_with_cookie_and_referer() -> None:
    headers = build_headers(cookie="a=1; b=2", referer="https://example.com/x")

    assert headers["Cookie"] == "a=1; b=2"
    assert headers["Referer"] == "https://example.com/x"


# ------------------------------------------------------------------
# detect_page_problem
# ------------------------------------------------------------------


def test_detect_page_problem_accepts_normal_article() -> None:
    assert detect_page_problem(_article_html()) is None


def test_detect_page_problem_ignores_block_words_inside_article_body() -> None:
    """正文里讨论"环境异常"不等于页面被风控——判定只看结构特征。"""
    html = _article_html(extra="<p>今天讲讲环境异常是怎么回事</p>")
    assert detect_page_problem(html) is None


@pytest.mark.parametrize(
    "marker",
    ["环境异常", "完成验证后即可继续访问", "请在微信客户端打开链接", "访问过于频繁"],
)
def test_detect_page_problem_flags_block_pages(marker: str) -> None:
    kind, reason = detect_page_problem(f"<html><body><p>{marker}</p></body></html>")

    assert kind == "blocked"
    assert reason == marker


@pytest.mark.parametrize(
    "marker",
    ["该内容已被发布者删除", "该链接已过期", "此内容因违规无法查看"],
)
def test_detect_page_problem_flags_unavailable_pages(marker: str) -> None:
    """内容失效时换浏览器也没用，必须与风控区分开。"""
    kind, reason = detect_page_problem(f"<html><body><p>{marker}</p></body></html>")

    assert kind == "unavailable"
    assert reason == marker


def test_detect_page_problem_flags_missing_content_container() -> None:
    kind, reason = detect_page_problem(
        '<html><h1 id="activity-name">t</h1>' + "x" * 50_000 + "</body></html>"
    )

    assert kind == "blocked"
    assert "js_content" in reason


def test_detect_page_problem_flags_missing_title_element() -> None:
    kind, reason = detect_page_problem(
        '<html><div id="js_content"></div>' + "x" * 50_000 + "</html>"
    )

    assert kind == "blocked"
    assert "activity-name" in reason


def test_detect_page_problem_flags_tiny_body() -> None:
    kind, reason = detect_page_problem(
        '<html><h1 id="activity-name">t</h1><div id="js_content"></div></html>'
    )

    assert kind == "blocked"
    assert "过小" in reason


@pytest.mark.parametrize("status", [401, 403, 418, 429])
def test_detect_page_problem_flags_client_block_status(status: int) -> None:
    kind, reason = detect_page_problem(_article_html(), status)

    assert kind == "blocked"
    assert reason == f"HTTP {status}"


def test_detect_page_problem_flags_server_error_as_retryable() -> None:
    kind, reason = detect_page_problem(_article_html(), 503)

    assert kind == "blocked"
    assert "503" in reason


def test_detect_page_problem_accepts_normal_article_with_200() -> None:
    assert detect_page_problem(_article_html(), 200) is None


# ------------------------------------------------------------------
# _parse_cookie_header
# ------------------------------------------------------------------


def test_parse_cookie_header_builds_playwright_cookies() -> None:
    cookies = _parse_cookie_header("a=1; b=2", "https://mp.weixin.qq.com/s/xyz")

    assert cookies == [
        {"name": "a", "value": "1", "domain": "mp.weixin.qq.com", "path": "/"},
        {"name": "b", "value": "2", "domain": "mp.weixin.qq.com", "path": "/"},
    ]


def test_parse_cookie_header_skips_junk_and_keeps_equals_in_value() -> None:
    cookies = _parse_cookie_header("bad; token=a=b; ; key=", "https://mp.weixin.qq.com/s/x")

    assert [c["name"] for c in cookies] == ["token", "key"]
    assert cookies[0]["value"] == "a=b"
    assert cookies[1]["value"] == ""


# ------------------------------------------------------------------
# find_local_chromium
# ------------------------------------------------------------------


def test_find_local_chromium_returns_existing_path_or_none() -> None:
    """不假设本机装没装 chromium，只要求返回值的形态正确。"""
    exe = find_local_chromium()

    if exe is None:
        pytest.skip("本机未安装 playwright chromium")
    assert exe.endswith(("chrome.exe", "chrome", "Chromium"))


# ------------------------------------------------------------------
# fetch_html 降级调度
# ------------------------------------------------------------------


BLOCK_PAGE = "<html><body><p>环境异常</p></body></html>"
DEAD_PAGE = "<html><body><p>该内容已被发布者删除</p></body></html>"


class _BackendSpy:
    """替换 requests / 浏览器后端，记录谁被调用过。"""

    def __init__(self, monkeypatch, requests_result, browser_result=None) -> None:
        self.calls: list[str] = []
        self._requests_result = requests_result
        self._browser_result = browser_result
        monkeypatch.setattr(wtm, "fetch_html_requests", self._requests)
        monkeypatch.setattr(wtm, "fetch_html_camoufox", self._camoufox)
        monkeypatch.setattr(wtm, "fetch_html_chromium", self._chromium)

    def _requests(self, url, **kwargs):
        self.calls.append("requests")
        if isinstance(self._requests_result, Exception):
            raise self._requests_result
        return self._requests_result

    async def _camoufox(self, url, **kwargs):
        self.calls.append("camoufox")
        return self._browser_result

    async def _chromium(self, url, **kwargs):
        self.calls.append("chromium")
        return self._browser_result


def test_auto_mode_skips_browser_when_requests_succeeds(monkeypatch) -> None:
    spy = _BackendSpy(monkeypatch, (("id=\"js_content\" id=\"activity-name\"" + "x" * 30_000), 200))

    html = asyncio.run(fetch_html(URL))

    assert spy.calls == ["requests"]
    assert "js_content" in html


def test_auto_mode_escalates_to_camoufox_when_blocked(monkeypatch) -> None:
    spy = _BackendSpy(
        monkeypatch,
        (BLOCK_PAGE, 200),
        browser_result="id=\"js_content\" id=\"activity-name\"" + "x" * 30_000,
    )

    html = asyncio.run(fetch_html(URL))

    assert spy.calls == ["requests", "camoufox"]
    assert "js_content" in html


def test_auto_mode_uses_requested_browser_engine(monkeypatch) -> None:
    spy = _BackendSpy(
        monkeypatch,
        (BLOCK_PAGE, 200),
        browser_result="id=\"js_content\" id=\"activity-name\"" + "x" * 30_000,
    )

    asyncio.run(fetch_html(URL, browser_engine="chromium"))

    assert spy.calls == ["requests", "chromium"]


def test_auto_mode_escalates_when_requests_raises(monkeypatch) -> None:
    spy = _BackendSpy(
        monkeypatch,
        RuntimeError("connection reset"),
        browser_result="id=\"js_content\" id=\"activity-name\"" + "x" * 30_000,
    )

    asyncio.run(fetch_html(URL))

    assert spy.calls == ["requests", "camoufox"]


def test_auto_mode_does_not_escalate_for_deleted_article(monkeypatch) -> None:
    """内容已删除时换浏览器也没用，应直接失败而不是白等一次浏览器启动。"""
    spy = _BackendSpy(monkeypatch, (DEAD_PAGE, 200), browser_result=None)

    with pytest.raises(RuntimeError, match="unavailable"):
        asyncio.run(fetch_html(URL))

    assert spy.calls == ["requests"]


def test_requests_only_mode_never_touches_browser(monkeypatch) -> None:
    spy = _BackendSpy(monkeypatch, (BLOCK_PAGE, 200), browser_result=None)

    with pytest.raises(RuntimeError, match="blocked"):
        asyncio.run(fetch_html(URL, mode="requests"))

    assert spy.calls == ["requests"]


def test_browser_only_mode_skips_requests(monkeypatch) -> None:
    spy = _BackendSpy(
        monkeypatch,
        (BLOCK_PAGE, 200),
        browser_result="id=\"js_content\" id=\"activity-name\"" + "x" * 30_000,
    )

    asyncio.run(fetch_html(URL, mode="browser"))

    assert spy.calls == ["camoufox"]


def test_auto_mode_fails_when_browser_also_blocked(monkeypatch) -> None:
    spy = _BackendSpy(monkeypatch, (BLOCK_PAGE, 200), browser_result=BLOCK_PAGE)

    with pytest.raises(RuntimeError, match="浏览器模式仍被拦截"):
        asyncio.run(fetch_html(URL))

    assert spy.calls == ["requests", "camoufox"]


def test_browser_fetch_honours_timeout(monkeypatch) -> None:
    """浏览器二进制卡死时必须按 --browser-timeout 报错，而不是无限等待。"""

    async def _hang(url, **kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(wtm, "fetch_html_requests", lambda url, **k: (BLOCK_PAGE, 200))
    monkeypatch.setattr(wtm, "fetch_html_camoufox", _hang)

    with pytest.raises(RuntimeError, match="未返回"):
        asyncio.run(fetch_html(URL, browser_timeout=0.05))
