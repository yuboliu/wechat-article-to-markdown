from __future__ import annotations

# /// script
# requires-python = ">=3.8"
# dependencies = [
#     "camoufox[geoip]",
#     "markdownify",
#     "beautifulsoup4",
#     "httpx",
#     "requests",
# ]
# ///

"""
WeChat Article to Markdown — 微信公众号文章抓取 & Markdown 转换工具

两级抓取策略（默认 ``auto``）：

1. **非浏览器模式（默认）** — 用 ``requests`` 直取 HTML。
   公众号正文是服务端渲染的，这条路拿得到完整内容，且比开浏览器快一个数量级、
   不吃内存。适合批量抓取。
2. **反爬模式（按需）** — 只有当第 1 步拿到的不是正常文章页（命中风控/验证页特征、
   缺少 ``#js_content`` 正文容器、响应体异常小）时，才自动升到浏览器模式，
   用 Camoufox（反检测 Firefox，默认）或 Chromium 重新渲染取 DOM。

拿到 HTML 后用 BeautifulSoup + markdownify 转成干净的 Markdown，图片自动下载到本地。
"""

import argparse
import asyncio
import html as html_mod
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import httpx
import markdownify
from bs4 import BeautifulSoup

# Default output directory (current working directory / output)
DEFAULT_OUTPUT_DIR = Path.cwd() / "output"
IMAGE_CONCURRENCY = 5


# ============================================================
# Helpers
# ============================================================


def normalize_wechat_url(raw: str) -> str:
    """Normalize a pasted WeChat article URL.

    Handles common issues:
    - Terminal/zsh auto-escaped backslashes (``\\&``, ``\\?``)
    - HTML entities (``&amp;``)
    - Missing or http scheme on mp.weixin.qq.com
    - Stray quote wrappers from copy-paste
    """
    s = str(raw or "").strip()
    if not s:
        return s

    # Strip wrapping quotes / angle brackets
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()
    if s.startswith("<") and s.endswith(">"):
        s = s[1:-1].strip()

    # Remove backslash escapes before URL-significant characters
    s = re.sub(r"\\+([:/&?=#%])", r"\1", s)

    # Decode HTML entities
    s = html_mod.unescape(s)

    # Allow bare hostnames
    if s.startswith("mp.weixin.qq.com/") or s.startswith("//mp.weixin.qq.com/"):
        s = "https://" + s.lstrip("/")

    # Force https for mp.weixin.qq.com
    parsed = urlparse(s)
    if parsed.scheme in ("http", "https") and (parsed.hostname or "").lower() == "mp.weixin.qq.com":
        s = urlunparse(("https", "mp.weixin.qq.com", parsed.path, parsed.params, parsed.query, parsed.fragment))

    return s


def extract_publish_time(html: str) -> str:
    """从 HTML script 标签中提取发布时间"""
    # JsDecode 格式
    m = re.search(r"create_time\s*:\s*JsDecode\('([^']+)'\)", html)
    if m:
        val = m.group(1)
        try:
            ts = int(val)
            if ts > 0:
                return format_timestamp(ts)
        except ValueError:
            return val

    # 纯数字格式
    m = re.search(r"create_time\s*:\s*'(\d+)'", html)
    if m:
        return format_timestamp(int(m.group(1)))

    # 兼容双引号与 = 赋值风格
    m = re.search(r'create_time\s*[:=]\s*["\']?(\d+)["\']?', html)
    if m:
        return format_timestamp(int(m.group(1)))

    return ""


def extract_source_url(html: str) -> str:
    """从页面内嵌脚本里取原文链接。

    用于 ``--html-file`` 离线解析、且调用方没给 URL 时兜底，保证成品的
    ``> 原文链接:`` 一行不丢。
    """
    m = re.search(r'var\s+msg_link\s*=\s*"([^"]+)"', html)
    if not m:
        m = re.search(r"var\s+msg_link\s*=\s*'([^']+)'", html)
    return html_mod.unescape(m.group(1)).strip() if m else ""


def format_timestamp(ts: int) -> str:
    """Unix timestamp (秒) -> 'YYYY-MM-DD HH:mm:ss' (Asia/Shanghai, UTC+8)"""
    from datetime import datetime, timezone, timedelta

    tz = timezone(timedelta(hours=8))
    dt = datetime.fromtimestamp(ts, tz=tz)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# Fetching (two-tier: plain HTTP first, browser only when blocked)
# ============================================================

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# 风控/验证页特征：命中说明服务端没给正文，可换浏览器模式重试
BLOCK_MARKERS = (
    "环境异常",
    "完成验证后即可继续访问",
    "去验证",
    "请在微信客户端打开链接",
    "访问过于频繁",
    "操作过于频繁",
    "wappoc_appmsgcaptcha",
    "verify_page",
    "请输入验证码",
)

# 内容本身已失效（删除 / 违规 / 过期）：换浏览器也没用，直接报错
UNAVAILABLE_MARKERS = (
    "该内容已被发布者删除",
    "该链接已过期",
    "此内容因违规无法查看",
    "该公众号已迁移",
)

# 正常文章页的 HTML 体量下限（公众号文章普遍 100KB+，验证页只有几 KB）
MIN_ARTICLE_HTML_CHARS = 20_000


def build_headers(cookie: str | None = None, referer: str | None = None) -> dict[str, str]:
    """构造与真实浏览器一致的请求头。

    刻意不设 ``Accept-Encoding``：交给 requests/urllib3 按已安装的解码器协商，
    避免声明 ``br`` 却没装 brotli 导致解码失败。
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": referer or "https://mp.weixin.qq.com/",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
    }
    if cookie:
        headers["Cookie"] = cookie
    return headers


def detect_page_problem(html: str, status_code: int | None = None) -> tuple[str, str] | None:
    """判断响应是正常文章页、风控页还是失效页。

    Returns:
        ``("unavailable", 原因)`` — 内容已失效，重试无意义
        ``("blocked", 原因)``     — 疑似风控/异常，可换浏览器模式重试
        ``None``                  — 看起来是正常文章页
    """
    if status_code is not None:
        if status_code in (401, 403, 418, 429):
            return ("blocked", f"HTTP {status_code}")
        if status_code >= 500:
            return ("blocked", f"HTTP {status_code}（服务端错误，可重试）")

    has_content = 'id="js_content"' in html or "id='js_content'" in html
    has_title = 'id="activity-name"' in html or "id='activity-name'" in html

    # 正文容器 + 标题元素 + 体量三者齐备 → 正常文章页。
    # 这里刻意**不扫正文关键词**：文章本身可能就在讨论"环境异常""访问过于频繁"
    # 这类话题，按关键词判会把正常文章误判成风控页（已有测试覆盖该场景）。
    if has_content and has_title and len(html) >= MIN_ARTICLE_HTML_CHARS:
        return None

    # 以下是「没拿到正常文章页」的情况，再用特征串区分「已失效」与「风控」
    for marker in UNAVAILABLE_MARKERS:
        if marker in html:
            return ("unavailable", marker)
    for marker in BLOCK_MARKERS:
        if marker in html:
            return ("blocked", marker)
    if not has_content:
        return ("blocked", "响应缺少正文容器 #js_content")
    if not has_title:
        return ("blocked", "响应缺少文章标题元素 #activity-name")
    return ("blocked", f"响应体过小（{len(html)} 字符）")


def _decode_response(resp) -> str:
    """按 header/meta 声明的编码解码，避免 requests 把中文页误判成 ISO-8859-1。"""
    ctype = (resp.headers.get("Content-Type") or "").lower()
    m = re.search(r"charset=([\w-]+)", ctype)
    if m:
        return resp.content.decode(m.group(1), errors="replace")

    m = re.search(rb"charset=[\"']?([\w-]+)", resp.content[:4096], re.IGNORECASE)
    if m:
        return resp.content.decode(m.group(1).decode("ascii", "ignore"), errors="replace")

    return resp.content.decode("utf-8", errors="replace")


def fetch_html_requests(
    url: str,
    *,
    timeout: float = 30.0,
    retries: int = 3,
    cookie: str | None = None,
    proxy: str | None = None,
    referer: str | None = None,
) -> tuple[str, int]:
    """非浏览器模式：requests 直取 HTML，返回 ``(html, status_code)``。

    只发一次 GET（带退避重试），不执行 JS。比开浏览器快一个数量级。
    """
    import requests
    from requests.adapters import HTTPAdapter

    session = requests.Session()
    session.headers.update(build_headers(cookie=cookie, referer=referer))
    try:
        try:
            from urllib3.util.retry import Retry

            retry = Retry(
                total=retries,
                connect=retries,
                read=retries,
                backoff_factor=0.8,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset({"GET"}),
                raise_on_status=False,
            )
            adapter = HTTPAdapter(max_retries=retry, pool_maxsize=4)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
        except ImportError:  # urllib3 异常时退化为无重试
            pass

        proxies = {"http": proxy, "https": proxy} if proxy else None
        resp = session.get(url, timeout=timeout, allow_redirects=True, proxies=proxies)
        return _decode_response(resp), resp.status_code
    finally:
        session.close()


def _parse_cookie_header(cookie_header: str, url: str) -> list[dict]:
    """``k=v; k2=v2`` → Playwright ``add_cookies()`` 需要的结构。"""
    host = urlparse(url).hostname or "mp.weixin.qq.com"
    cookies = []
    for part in cookie_header.split(";"):
        if "=" not in part:
            continue
        name, _, value = part.strip().partition("=")
        if not name:
            continue
        cookies.append({"name": name, "value": value, "domain": host, "path": "/"})
    return cookies


def find_local_chromium() -> str | None:
    """探测本机已安装的 Chromium 可执行文件（取 revision 最大的）。

    Playwright 各版本要求的 chromium revision 不同；显式指定可执行文件可以避开
    ``Executable doesn't exist`` 的版本不匹配。
    """
    base = Path(os.environ.get("LOCALAPPDATA", "") or (Path.home() / "AppData" / "Local"))
    base = base / "ms-playwright"
    if not base.is_dir():
        return None

    candidates: list[tuple[int, Path]] = []
    for d in base.glob("chromium-*"):
        for rel in (
            "chrome-win64/chrome.exe",
            "chrome-win/chrome.exe",
            "chrome-linux/chrome",
            "chrome-mac/Chromium.app/Contents/MacOS/Chromium",
        ):
            exe = d / rel
            if exe.exists():
                m = re.search(r"chromium-(\d+)", d.name)
                candidates.append((int(m.group(1)) if m else 0, exe))
                break
    if not candidates:
        return None
    return str(max(candidates, key=lambda x: x[0])[1])


async def fetch_html_camoufox(
    url: str,
    *,
    cookie: str | None = None,
    proxy: str | None = None,
) -> str:
    """反爬模式（默认引擎）：用 Camoufox 反检测 Firefox 渲染后取 DOM。"""
    try:
        from camoufox.async_api import AsyncCamoufox
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "camoufox 未安装；如只需浏览器渲染，可改用 --browser-engine chromium"
        ) from exc

    kwargs: dict = {"headless": True}
    if proxy:
        kwargs["proxy"] = {"server": proxy}

    async with AsyncCamoufox(**kwargs) as browser:
        contexts = browser.contexts
        ctx = contexts[0] if contexts else await browser.new_context()
        if cookie:
            await ctx.add_cookies(_parse_cookie_header(cookie, url))
        page = await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded")
        try:
            await page.wait_for_selector("#js_content", timeout=10_000)
        except Exception:  # noqa: BLE001 - 超时也继续，交给解析层判断
            pass
        await asyncio.sleep(2)  # 等正文与图片懒加载填好
        return await page.content()


async def fetch_html_chromium(
    url: str,
    *,
    cookie: str | None = None,
    proxy: str | None = None,
) -> str:
    """反爬模式备选引擎：Playwright + Chromium。

    Camoufox 不可用（如二进制无法被驱动）时使用，产物与 Camoufox 路径等价。
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "playwright 未安装；请先 `pip install playwright` 并 `playwright install chromium`"
        ) from exc

    kwargs: dict = {
        "headless": True,
        "args": ["--no-sandbox", "--disable-dev-shm-usage"],
    }
    exe = find_local_chromium()
    if exe:
        kwargs["executable_path"] = exe
    if proxy:
        kwargs["proxy"] = {"server": proxy}

    async with async_playwright() as p:
        browser = await p.chromium.launch(**kwargs)
        try:
            ctx = await browser.new_context(locale="zh-CN", user_agent=USER_AGENT)
            if cookie:
                await ctx.add_cookies(_parse_cookie_header(cookie, url))
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            try:
                await page.wait_for_selector("#js_content", timeout=20_000)
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(1.5)
            return await page.content()
        finally:
            await browser.close()


async def _with_timeout(coro, seconds: float, label: str):
    """给浏览器抓取加硬超时，避免二进制卡死时无限等待。"""
    try:
        return await asyncio.wait_for(coro, timeout=seconds)
    except asyncio.TimeoutError:
        raise RuntimeError(
            f"{label} 在 {seconds:.0f}s 内未返回（浏览器可能无法启动）；"
            "可试 --browser-engine chromium，或检查浏览器安装"
        ) from None


async def fetch_html(
    url: str,
    *,
    mode: str = "auto",
    browser_engine: str = "camoufox",
    cookie: str | None = None,
    proxy: str | None = None,
    timeout: float = 30.0,
    retries: int = 3,
    browser_timeout: float = 90.0,
) -> str:
    """两级抓取入口。

    mode:
      - ``auto``     先 requests，命中风控才升到浏览器（默认）
      - ``requests`` 只用 requests，异常即报错（最快）
      - ``browser``  只走浏览器（旧版行为）
    """
    if mode in ("auto", "requests"):
        try:
            html, status = fetch_html_requests(
                url, timeout=timeout, retries=retries, cookie=cookie, proxy=proxy
            )
        except Exception as exc:  # noqa: BLE001
            if mode == "requests":
                raise
            print(f"⚠️  requests 抓取失败（{type(exc).__name__}: {exc}）")
            html = ""
        else:
            print(f"🌐 requests 取回 HTML: {len(html)} 字符 (HTTP {status})")
            problem = detect_page_problem(html, status)
            if problem is None:
                print("✅ 非浏览器模式命中正文，跳过浏览器")
                return html

            kind, reason = problem
            print(f"⚠️  响应不是正常文章页（{kind}: {reason}）")
            if kind == "unavailable" or mode == "requests":
                raise RuntimeError(f"页面不可用（{kind}: {reason}）")

        print(f"🔁 转入反爬模式（--browser-engine {browser_engine}）")

    if browser_engine == "chromium":
        html = await _with_timeout(
            fetch_html_chromium(url, cookie=cookie, proxy=proxy), browser_timeout, "chromium"
        )
    else:
        html = await _with_timeout(
            fetch_html_camoufox(url, cookie=cookie, proxy=proxy), browser_timeout, browser_engine
        )

    print(f"🌐 {browser_engine} 取回 HTML: {len(html)} 字符")
    problem = detect_page_problem(html)
    if problem is not None:
        raise RuntimeError(f"浏览器模式仍被拦截（{problem[0]}: {problem[1]}）")
    return html


# ============================================================
# Image Downloading
# ============================================================


async def download_image(
    client: httpx.AsyncClient,
    img_url: str,
    img_dir: Path,
    index: int,
    semaphore: asyncio.Semaphore,
) -> tuple[str, str | None]:
    """下载单张图片到本地，返回 (remote_url, local_relative_path | None)"""
    async with semaphore:
        try:
            url = img_url if not img_url.startswith("//") else f"https:{img_url}"

            # 推断扩展名
            ext_match = re.search(r"wx_fmt=(\w+)", url) or re.search(
                r"\.(\w{3,4})(?:\?|$)", url
            )
            ext = ext_match.group(1) if ext_match else "png"

            filename = f"img_{index:03d}.{ext}"
            filepath = img_dir / filename

            resp = await client.get(
                url,
                headers={"Referer": "https://mp.weixin.qq.com/"},
                timeout=15.0,
            )
            resp.raise_for_status()
            filepath.write_bytes(resp.content)
            return img_url, f"images/{filename}"
        except Exception as e:
            print(f"  ⚠ 图片下载失败: {e}")
            return img_url, None


async def download_all_images(
    img_urls: list[str], img_dir: Path
) -> dict[str, str]:
    """并发下载所有图片，返回 {remote_url: local_path} 映射"""
    if not img_urls:
        return {}

    print(f"🖼  下载 {len(img_urls)} 张图片 (并发 {IMAGE_CONCURRENCY})...")
    semaphore = asyncio.Semaphore(IMAGE_CONCURRENCY)

    async with httpx.AsyncClient() as client:
        tasks = [
            download_image(client, url, img_dir, i + 1, semaphore)
            for i, url in enumerate(img_urls)
        ]
        results = await asyncio.gather(*tasks)

    url_map = {}
    for remote_url, local_path in results:
        if local_path:
            url_map[remote_url] = local_path

    downloaded = sum(1 for v in url_map.values() if v)
    print(f"  ✅ {downloaded}/{len(img_urls)}")
    return url_map


# ============================================================
# Content Processing
# ============================================================


def extract_metadata(soup: BeautifulSoup, html: str) -> dict:
    """提取文章元数据: 标题、作者、发布时间"""
    title_el = soup.select_one("#activity-name")
    author_el = soup.select_one("#js_name")
    return {
        "title": title_el.get_text(strip=True) if title_el else "",
        "author": author_el.get_text(strip=True) if author_el else "",
        "publish_time": extract_publish_time(html),
    }


def process_content(soup: BeautifulSoup) -> tuple[str, list[dict], list[str]]:
    """
    预处理正文 DOM：修复图片、处理代码块、移除噪声元素。
    返回 (content_html, code_blocks, img_urls)
    """
    content_el = soup.select_one("#js_content")
    if not content_el:
        return "", [], []

    # 1) 图片: data-src -> src (微信懒加载)
    for img in content_el.find_all("img"):
        data_src = img.get("data-src")
        if data_src:
            img["src"] = data_src

    # 2) 小标题：微信把章节标题写成样式块，例如
    #      <section style="font-size: 17px;font-weight: bold;color: #ff6600;text-align: center">
    #        <span leaf="">AI推理流量四大核心特征</span></section>
    #    markdownify 不认识它，会退化成一行普通段落，落库后整篇没有 ## 层级。
    #    确定性判定：内联样式含 font-weight:bold 且 font-size >= 16px（正文是 15px）、
    #    纯文本、长度 <= 40 字、不以句末标点收尾 → 提升为 <h2>。
    #    注意：L1(17px 橙) 与 L2(16px 蓝) 统一压成一级 ##，与既有笔记「H1 + 单层 ##」一致。
    for sec in content_el.find_all("section"):
        style = (sec.get("style") or "").replace(" ", "").lower()
        if "font-weight:bold" not in style and "font-weight:700" not in style:
            continue
        size_m = re.search(r"font-size:(\d+(?:\.\d+)?)px", style)
        if not size_m or float(size_m.group(1)) < 16:
            continue
        if sec.find(["section", "img", "p", "div", "table", "ul", "ol"]):
            continue  # 容器型 section，不是标题
        sec_text = sec.get_text(strip=True)
        if not sec_text or len(sec_text) > 40 or sec_text[-1] in "。！？；：,!?;":
            continue
        h2 = soup.new_tag("h2")
        h2.string = sec_text
        sec.replace_with(h2)

    # 3) 代码块: 提取 code-snippet__fix 内容，替换为占位符
    code_blocks = []
    for el in content_el.select(".code-snippet__fix"):
        # 移除行号
        for line_idx in el.select(".code-snippet__line-index"):
            line_idx.decompose()

        pre = el.select_one("pre[data-lang]")
        lang = pre.get("data-lang", "") if pre else ""

        lines = []
        for code_tag in el.find_all("code"):
            text = code_tag.get_text()
            # 跳过 CSS counter 泄漏的垃圾行
            if re.match(r"^[ce]?ounter\(line", text):
                continue
            lines.append(text)

        if not lines:
            lines.append(el.get_text())

        placeholder = f"CODEBLOCK-PLACEHOLDER-{len(code_blocks)}"
        code_blocks.append({"lang": lang, "code": "\n".join(lines)})
        el.replace_with(soup.new_tag("p", string=placeholder))

    # 4) 移除噪声元素
    for sel in ("script", "style", ".qr_code_pc", ".reward_area"):
        for tag in content_el.select(sel):
            tag.decompose()

    # 5) 收集图片 URL（去重）
    img_urls = []
    seen = set()
    for img in content_el.find_all("img", src=True):
        src = img["src"]
        if src not in seen:
            seen.add(src)
            img_urls.append(src)

    return str(content_el), code_blocks, img_urls


def convert_to_markdown(content_html: str, code_blocks: list[dict]) -> str:
    """HTML -> Markdown，还原代码块，清理格式"""
    md = markdownify.markdownify(
        content_html,
        heading_style="ATX",
        bullets="-",
        convert=["p", "h1", "h2", "h3", "h4", "h5", "h6",
                 "strong", "em", "a", "img", "ul", "ol", "li",
                 "blockquote", "br", "hr", "table", "thead",
                 "tbody", "tr", "th", "td", "pre", "code"],
    )

    # 还原代码块占位符
    for i, block in enumerate(code_blocks):
        placeholder = f"CODEBLOCK-PLACEHOLDER-{i}"
        fenced = f"\n```{block['lang']}\n{block['code']}\n```\n"
        md = md.replace(placeholder, fenced)

    # 清理 &nbsp; 残留
    md = md.replace("\u00a0", " ")
    # 清理多余空行
    md = re.sub(r"\n{4,}", "\n\n\n", md)
    # 清理行尾多余空格
    md = re.sub(r"[ \t]+$", "", md, flags=re.MULTILINE)

    return md


def replace_image_urls(md: str, url_map: dict[str, str]) -> str:
    """替换 Markdown 中的远程图片链接为本地路径"""
    # Use exact URL matching to avoid regex edge cases such as ')' in URL.
    for remote_url, local_path in url_map.items():
        pattern = re.compile(r"!\[([^\]]*)\]\(" + re.escape(remote_url) + r"\)")
        md = pattern.sub(lambda m: f"![{m.group(1)}]({local_path})", md)
    return md


def build_markdown(meta: dict, body_md: str) -> str:
    """拼接最终 Markdown 文件内容"""
    lines = [f"# {meta['title']}", ""]
    if meta.get("author"):
        lines.append(f"> 公众号: {meta['author']}")
    if meta.get("publish_time"):
        lines.append(f"> 发布时间: {meta['publish_time']}")
    if meta.get("source_url"):
        lines.append(f"> 原文链接: {meta['source_url']}")
    if meta.get("author") or meta.get("publish_time") or meta.get("source_url"):
        lines.append("")
    lines.extend(["---", ""])
    return "\n".join(lines) + body_md


# ============================================================
# Main
# ============================================================


async def fetch_article(
    url: str,
    output_dir: Path | None = None,
    *,
    mode: str = "auto",
    browser_engine: str = "camoufox",
    cookie: str | None = None,
    proxy: str | None = None,
    timeout: float = 30.0,
    retries: int = 3,
    browser_timeout: float = 90.0,
    html_file: Path | None = None,
    save_html: Path | None = None,
) -> None:
    """
    抓取微信公众号文章并转换为 Markdown。

    Args:
        url: 微信文章 URL
        output_dir: 输出目录，默认为 DEFAULT_OUTPUT_DIR
        mode: ``auto``（先 requests，命中风控再升浏览器）/ ``requests`` / ``browser``
        browser_engine: 浏览器模式用 ``camoufox``（默认）或 ``chromium``
        cookie: 附加 Cookie 头（受限文章可粘贴登录态）
        proxy: HTTP(S) 代理
        timeout: requests 单次请求超时（秒）
        retries: requests 重试次数
        browser_timeout: 浏览器模式整体超时（秒），防止二进制卡死时无限等待
        html_file: 直接解析本地已保存的 HTML，完全不发网络请求
        save_html: 把取回的原始 HTML 另存一份（便于离线复跑 / 排查风控）
    """
    if output_dir is None:
        output_dir = DEFAULT_OUTPUT_DIR

    if html_file is not None:
        html = Path(html_file).read_text(encoding="utf-8")
        print(f"🔄 从本地 HTML 解析: {html_file}（{len(html)} 字符）")
    else:
        print(f"🔄 正在抓取: {url}")
        html = await fetch_html(
            url,
            mode=mode,
            browser_engine=browser_engine,
            cookie=cookie,
            proxy=proxy,
            timeout=timeout,
            retries=retries,
            browser_timeout=browser_timeout,
        )

    if save_html is not None:
        save_path = Path(save_html)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(html, encoding="utf-8")
        print(f"💾 原始 HTML 已另存: {save_path}")

    # 解析
    soup = BeautifulSoup(html, "html.parser")

    # 提取元数据
    meta = extract_metadata(soup, html)
    if not meta["title"]:
        print("❌ 未能提取到文章标题，可能触发了验证码")
        output_dir.mkdir(parents=True, exist_ok=True)
        debug_path = output_dir / "debug.html"
        debug_path.write_text(html, encoding="utf-8")
        print(f"已保存原始 HTML 到 {debug_path}")
        sys.exit(1)

    meta["source_url"] = url or extract_source_url(html)
    print(f"📄 标题: {meta['title']}")
    print(f"👤 作者: {meta['author']}")
    print(f"📅 时间: {meta['publish_time']}")

    # 处理正文
    content_html, code_blocks, img_urls = process_content(soup)
    if not content_html:
        print("❌ 未能提取到正文内容")
        sys.exit(1)

    # 转 Markdown
    md = convert_to_markdown(content_html, code_blocks)

    # 下载图片
    safe_title = re.sub(r'[/\\?%*:|"<>]', "_", meta["title"])[:80]
    article_dir = output_dir / safe_title
    img_dir = article_dir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)

    url_map = await download_all_images(img_urls, img_dir)
    md = replace_image_urls(md, url_map)

    # 写入文件
    result = build_markdown(meta, md)
    md_path = article_dir / f"{safe_title}.md"
    md_path.write_text(result, encoding="utf-8")

    print(f"✅ 已保存: {md_path}")
    print(f"📊 Markdown 约 {len(md)} 字符")


def main():
    parser = argparse.ArgumentParser(
        description="微信公众号文章抓取 & Markdown 转换工具（默认 requests 直取，遇风控自动升浏览器）"
    )
    parser.add_argument("url", nargs="?", help="微信公众号文章 URL")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"输出目录 (默认: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--mode",
        choices=("auto", "requests", "browser"),
        default="auto",
        help="抓取模式: auto=先 requests 再按需升浏览器 (默认); requests=只用 requests; browser=只用浏览器",
    )
    parser.add_argument(
        "--browser-engine",
        choices=("camoufox", "chromium"),
        default="camoufox",
        help="浏览器模式使用的引擎 (默认: camoufox)",
    )
    parser.add_argument(
        "--html-file",
        type=Path,
        help="直接解析本地已保存的 HTML（不发网络请求；建议同时给出 URL 以记录原文链接）",
    )
    parser.add_argument(
        "--save-html",
        type=Path,
        help="把取回的原始 HTML 另存一份（便于离线复跑 / 排查风控）",
    )
    parser.add_argument("--cookie", help="附加 Cookie 头（受限文章可粘贴登录态 Cookie）")
    parser.add_argument("--proxy", help="HTTP(S) 代理，如 http://127.0.0.1:7890")
    parser.add_argument(
        "--timeout", type=float, default=30.0, help="requests 单次请求超时秒数 (默认: 30)"
    )
    parser.add_argument("--retries", type=int, default=3, help="requests 重试次数 (默认: 3)")
    parser.add_argument(
        "--browser-timeout",
        type=float,
        default=90.0,
        help="浏览器模式整体超时秒数 (默认: 90)",
    )

    args = parser.parse_args()
    raw_url = args.url
    url = normalize_wechat_url(raw_url) if raw_url else ""
    if raw_url and url != raw_url:
        print("ℹ️  已自动清理 URL 中的转义字符 / HTML 实体。")

    if args.html_file is None and not url.startswith("https://mp.weixin.qq.com/"):
        print("❌ 请输入有效的微信文章 URL (mp.weixin.qq.com)")
        print("提示：请用引号包住完整 URL；若粘贴后出现反斜杠转义，脚本会自动清理。")
        print("     也可用 --html-file 直接解析本地已保存的 HTML。")
        sys.exit(1)

    try:
        asyncio.run(
            fetch_article(
                url,
                output_dir=args.output,
                mode=args.mode,
                browser_engine=args.browser_engine,
                cookie=args.cookie,
                proxy=args.proxy,
                timeout=args.timeout,
                retries=args.retries,
                browser_timeout=args.browser_timeout,
                html_file=args.html_file,
                save_html=args.save_html,
            )
        )
    except SystemExit:
        raise
    except Exception as e:
        print(f"❌ 抓取失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
