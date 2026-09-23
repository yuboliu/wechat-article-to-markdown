# wechat-article-to-markdown

Fetch WeChat Official Account articles and convert them to clean Markdown.

[English](#features) | [中文](#功能特性)

## Features

- Two-tier fetching: plain HTTP (`requests`) first, browser only when blocked
- Browser fallback via Camoufox (anti-detection) or Chromium
- Extract article metadata (title, account name, publish time, source URL)
- Convert WeChat article HTML to Markdown
- Download article images to local `images/` and rewrite links
- Handle WeChat `code-snippet` blocks with language fences
- Promote WeChat styled-block headings (`<section style="font-weight:bold">`) to `##`

## Installation

```bash
# Recommended: uv tool (fast, isolated)
uv tool install wechat-article-to-markdown

# Or: pipx
pipx install wechat-article-to-markdown
```

Or from source:

```bash
git clone git@github.com:jackwener/wechat-article-to-markdown.git
cd wechat-article-to-markdown
uv sync
```

## Usage

```bash
# Installed CLI
wechat-article-to-markdown "https://mp.weixin.qq.com/s/xxxxxxxx"

# Run in repo with uv
uv run wechat-article-to-markdown "https://mp.weixin.qq.com/s/xxxxxxxx"

# Backward-compatible local entry
uv run main.py "https://mp.weixin.qq.com/s/xxxxxxxx"
```

Output structure:

```text
output/
└── <article-title>/
    ├── <article-title>.md
    └── images/
        ├── img_001.png
        ├── img_002.png
        └── ...
```

### Fetch modes

By default the tool is **two-tier**: it fetches the HTML with `requests` first
(no browser), and only escalates to a browser when the response is not a normal
article page (missing `#js_content` / `#activity-name`, HTTP 403/429/5xx,
anti-bot markers, abnormally small body).

```bash
# Default: plain HTTP, escalate to browser only if blocked
wechat-article-to-markdown "https://mp.weixin.qq.com/s/xxxxxxxx"

# Force plain HTTP only (fastest, no browser); fail fast if blocked
wechat-article-to-markdown "https://mp.weixin.qq.com/s/xxxxxxxx" --mode requests

# Force browser only (legacy behaviour)
wechat-article-to-markdown "https://mp.weixin.qq.com/s/xxxxxxxx" --mode browser

# Pick the browser engine used for the fallback (default: camoufox)
wechat-article-to-markdown "https://mp.weixin.qq.com/s/xxxxxxxx" --browser-engine chromium

# Restricted article: attach cookies / go through a proxy
wechat-article-to-markdown "https://mp.weixin.qq.com/s/xxxxxxxx" --cookie "k=v; k2=v2" --proxy http://127.0.0.1:7890

# Save the raw HTML, then re-parse it offline later
wechat-article-to-markdown "https://mp.weixin.qq.com/s/xxxxxxxx" --save-html raw.html
wechat-article-to-markdown --html-file raw.html -o output
```

Both paths produce **byte-identical** Markdown, so downstream tooling needs no changes.


## Testing

```bash
# Unit tests (default CI path)
uv run --with pytest pytest -q -m "not e2e"

# Live E2E against real WeChat articles
WECHAT_E2E_URLS="https://mp.weixin.qq.com/s/Y7dyRC7CJ09miHWU6LBzBA,https://mp.weixin.qq.com/s/xxxxxxxx" \
  uv run --with pytest pytest -q -m e2e -s
```

`e2e` tests require network and browser runtime, so they run via manual GitHub Actions workflow `.github/workflows/e2e.yml`.

## Use as AI Agent Skill

This project ships with [`SKILL.md`](./SKILL.md), so AI agents can discover and use this tool workflow.

### [Skills CLI](https://github.com/vercel-labs/skills) (Recommended)

```bash
npx skills add jackwener/wechat-article-to-markdown
```

| Flag | Description |
| --- | --- |
| `-g` | Install globally (user-level, shared across projects) |
| `-a claude-code` | Target a specific agent |
| `-y` | Non-interactive mode |

### Manual Install

```bash
mkdir -p .agents/skills
git clone git@github.com:jackwener/wechat-article-to-markdown.git \
  .agents/skills/wechat-article-to-markdown
```

```bash
# Claude Code user-level skills directory (global)
mkdir -p ~/.claude/skills/wechat-article-to-markdown
curl -o ~/.claude/skills/wechat-article-to-markdown/SKILL.md \
  https://raw.githubusercontent.com/jackwener/wechat-article-to-markdown/main/SKILL.md
```

After adding the file, restart Claude Code to reload skills.

### ~~OpenClaw / ClawHub~~ (Deprecated)

> ⚠️ ClawHub install method is deprecated and no longer supported. Use [Skills CLI](#skills-cli-recommended) or Manual Install above.

## PyPI Publishing (GitHub Actions)

Repository: `jackwener/wechat-article-to-markdown`
Workflow: `.github/workflows/release.yml`
Environment: `pypi`

`release.yml` triggers on `v*` tags, runs unit tests + live e2e tests, then publishes to PyPI with trusted publishing (`id-token: write`).

For release e2e targets, set repository variable `RELEASE_E2E_URLS` (comma-separated article URLs).  
If not set, workflow falls back to `https://mp.weixin.qq.com/s/Y7dyRC7CJ09miHWU6LBzBA`.

---

## 功能特性

- 两级抓取：默认 `requests` 直取，命中风控才升浏览器
- 浏览器兜底支持 Camoufox（反检测）与 Chromium
- 提取标题、公众号名称、发布时间、原文链接
- 将微信公众号文章 HTML 转换为 Markdown
- 下载图片到本地 `images/` 并自动替换链接
- 处理微信 `code-snippet` 代码块并保留语言标识
- 把微信样式块小标题（`<section style="font-weight:bold">`）提升为 `##`

## 安装

```bash
# 推荐：uv tool
uv tool install wechat-article-to-markdown

# 或者：pipx
pipx install wechat-article-to-markdown
```

## 使用示例

```bash
wechat-article-to-markdown "https://mp.weixin.qq.com/s/xxxxxxxx"
```

### 抓取模式

默认是**两级策略**：先用 `requests` 直取 HTML，只有当响应不是正常文章页
（缺 `#js_content` / `#activity-name`、HTTP 403/429/5xx、命中风控特征串、响应体异常小）
时才自动升到浏览器模式重抓。

| 模式 | 行为 |
| --- | --- |
| `--mode auto`（默认） | 先 requests；命中风控才升浏览器 |
| `--mode requests` | 只用 requests，异常即失败（最快） |
| `--mode browser` | 只用浏览器（旧版行为） |

```bash
# 强制非浏览器模式（最快）
wechat-article-to-markdown "<URL>" --mode requests

# 浏览器兜底换用 Chromium
wechat-article-to-markdown "<URL>" --browser-engine chromium

# 受限文章：带 Cookie / 走代理
wechat-article-to-markdown "<URL>" --cookie "k=v; k2=v2" --proxy http://127.0.0.1:7890

# 先存原始 HTML，之后离线复跑
wechat-article-to-markdown "<URL>" --save-html raw.html
wechat-article-to-markdown --html-file raw.html -o output
```

两种模式**产物逐字节一致**，下游工具无需改动。

## 作为 AI Agent Skill 使用

项目自带 [`SKILL.md`](./SKILL.md)，可供支持 `.agents/skills/` 约定的 Agent 自动发现。

### [Skills CLI](https://github.com/vercel-labs/skills)（推荐）

```bash
npx skills add jackwener/wechat-article-to-markdown
```

| 参数 | 说明 |
| --- | --- |
| `-g` | 全局安装（用户级别，跨项目共享） |
| `-a claude-code` | 指定目标 Agent |
| `-y` | 非交互模式 |

### 手动安装

```bash
mkdir -p ~/.claude/skills/wechat-article-to-markdown
curl -o ~/.claude/skills/wechat-article-to-markdown/SKILL.md \
  https://raw.githubusercontent.com/jackwener/wechat-article-to-markdown/main/SKILL.md
```

### ~~OpenClaw / ClawHub~~（已过时）

> ⚠️ ClawHub 安装方式已过时，不再支持。请使用上方的 Skills CLI 或手动安装。

## License

MIT
