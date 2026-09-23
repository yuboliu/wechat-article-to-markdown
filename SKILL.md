---
name: wechat-article-to-markdown
description: Fetch WeChat Official Account (微信公众号) articles from mp.weixin.qq.com and convert to Markdown. 微信文章转 Markdown 工具。
author: jackwener
version: "1.0.0"
tags:
  - wechat
  - 微信
  - 微信文章
  - 公众号
  - mp.weixin.qq.com
  - markdown
  - article
  - converter
  - cli
---

# WeChat Article to Markdown

Fetch a WeChat Official Account article and convert it to a clean Markdown file.

## When to use

Use this skill when you need to save WeChat articles as Markdown for:
- Personal archive
- AI summarization input
- Knowledge base ingestion

## Prerequisites

- Python 3.8+

```bash
# Install
uv tool install wechat-article-to-markdown
# Or: pipx install wechat-article-to-markdown
```

## Usage

```bash
wechat-article-to-markdown "<WECHAT_ARTICLE_URL>"
```

### Fetch modes（两级抓取）

默认是**两级策略**：先用 `requests` 直取 HTML（非浏览器模式），只有当响应不是正常
文章页时才自动升到浏览器模式重抓。

判定"不是正常文章页"的依据（`detect_page_problem()`）：

- HTTP 401 / 403 / 418 / 429 / 5xx
- 缺 `#js_content`（正文容器）或 `#activity-name`（标题元素）
- 响应体异常小（< 20 000 字符）
- 命中风控特征串（`环境异常`、`完成验证后即可继续访问`、`请在微信客户端打开链接` …）

判定**刻意不看正文关键词**：文章本身可能在讨论"环境异常""访问过于频繁"，按关键词
判会把正常文章误判成风控页。

| 模式 | 行为 | 适用 |
| --- | --- | --- |
| `--mode auto`（默认） | 先 requests；命中风控才升浏览器 | 日常 |
| `--mode requests` | 只用 requests，异常即失败（最快、省内存） | 批量抓取 |
| `--mode browser` | 只用浏览器（旧版行为） | 明确知道必须靠渲染 |

`--browser-engine camoufox|chromium`（默认 `camoufox`）选择兜底引擎。

其他参数：

| 参数 | 说明 |
| --- | --- |
| `--cookie "<k=v; k2=v2>"` | 附加 Cookie（需要登录态的受限文章） |
| `--proxy http://127.0.0.1:7890` | 走代理 |
| `--timeout` / `--retries` | requests 超时与重试（默认 30s / 3 次） |
| `--browser-timeout` | 浏览器模式整体硬超时（默认 90s），防止引擎卡死时无限等待 |
| `--save-html raw.html` | 把取回的原始 HTML 另存一份，便于离线复跑 / 排查风控 |
| `--html-file raw.html` | 直接解析本地 HTML，完全不发网络请求 |

两种模式**产物逐字节一致**（同一篇文章实测 md5 相同），下游转换/校验/落库零改动。

内容已失效（删除 / 违规 / 过期）时报 `unavailable` 且**不升浏览器** —— 重试无意义。

Input URL format:
- `https://mp.weixin.qq.com/s/...`

Output files:
- `<cwd>/output/<article-title>/<article-title>.md`
- `<cwd>/output/<article-title>/images/*`

## 本机运行方式（本地适配；上游是 `uv tool install` 的全局 CLI）

本机没有全局 `wechat-article-to-markdown` 命令，用户级 skill 是上游仓库的 git clone，
自带 `.venv`，直接跑源码即可（依赖已装好：camoufox / requests / httpx / markdownify /
beautifulsoup4 / playwright）。

```bash
# SK 指向 skill 目录（Windows 下即 C:/Users/<你>/.workbuddy/skills/wechat-article-to-markdown）
SK="$HOME/.workbuddy/skills/wechat-article-to-markdown"
"$SK/.venv/Scripts/python.exe" "$SK/wechat_article_to_markdown.py" "<URL>" -o "<输出根目录>"
```

只想走最快的非浏览器模式：

```bash
"$SK/.venv/Scripts/python.exe" "$SK/wechat_article_to_markdown.py" "<URL>" \
  -o "<输出根目录>" --mode requests
```

> ⚠️ **本机 camoufox 二进制无法被自动化驱动**（2026-09-23 定位，勿再重复试"换版本"）。
> 症状：浏览器进程能起，但 chrome window 初始化不完成，Juggler 15s 后抛
> `chrome://juggler/content/TargetRegistry.js:227 Error: gBrowser never populated`。
> 已排除：build 版本（beta.29/beta.30）、playwright 版本（1.60/1.62）、camoufox 封装
> （原生通道直 launch 同样失败）、沙箱、e10s / 内容沙箱 / WebRender / Skeleton UI。
> 决定性对照：同一 playwright 驱动**官方 firefox → 1.9s PASS**、**Chromium → PASS**
> ⇒ 环境正常，问题在 camoufox 二进制侧。
>
> ⇒ **本机需要浏览器兜底时请加 `--browser-engine chromium`**。
> `--browser-timeout`（默认 90s）已做硬超时，不会像旧版那样卡死。

## 小标题层级（样式块提升为 `##`）

微信大量文章的章节标题是**样式块**而不是 `<h1-6>`：

```html
<section style="font-size: 17px;font-weight: bold;color: #ff6600;text-align: center">
  <span leaf="">AI推理流量四大核心特征</span></section>
```

markdownify 不认识它 → 退化成普通段落 → 笔记整篇没有 `##` 层级。

`process_content()` 里做了确定性判定（`# 2) 小标题`）：内联样式含 `font-weight: bold`
且 `font-size >= 16px`（正文 15px）、纯文本、≤40 字、不以 `。！？；：` 收尾 → 提升为
`<h2>`。L1(17px) 与 L2(16px) 统一压成一级 `##`。

## Features

1. Two-tier fetching: plain HTTP (`requests`) first, browser only when blocked
2. Browser fallback via Camoufox (anti-detection) or Chromium
3. Metadata extraction (title, account name, publish time, source URL)
4. Image localization to local files
5. WeChat code-snippet extraction and fenced code block output
6. HTML to Markdown conversion via markdownify
7. Concurrent image downloading
8. Styled-block headings promoted to `##`

## Limitations

- Some code snippets are image/SVG rendered and cannot be extracted as source code
- Public `mp.weixin.qq.com` URL is required
