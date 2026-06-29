"""Detect input type (URL / markdown / free text) and normalize to plain text.

Returns ``(source_type, normalized_text, meta)`` where ``source_type`` is one
of ``{"url", "markdown", "text"}``.

For URL inputs the meta dict carries the source URL, domain, og:image,
additional inline article images, and (for GitHub URLs) repo stats fetched
from the GitHub API. The planner consumes the appended stats block so it can
quote exact numbers rather than guess.

X/Twitter URLs are special-cased: the HTML is unreachable behind the login
wall so the fetch is skipped and a stub describing the tweet is returned for
the planner to handle via Google Search grounding.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .github import fetch_github_stats, format_github_stats_block

logger = logging.getLogger("briefa.sources.url_extract")

SourceType = Literal["url", "markdown", "text"]

# A *single* URL on its own line.
_URL_RE = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)

# Find URLs inside arbitrary text (first match wins).
_URL_INLINE_RE = re.compile(r"\bhttps?://[^\s<>\")]+", re.IGNORECASE)

# Tags to strip before extracting article text.
_STRIP_TAGS = ("script", "style", "nav", "footer", "header", "aside", "noscript")

# Gemini context budget — article text is truncated to this before injection.
_MAX_CHARS = 8000

# Fetch timeout for article HTML.
_FETCH_TIMEOUT = 20.0

# Realistic Chrome UA — some VN news sites 403 on anything that looks like a
# bot. We still identify ourselves in the ``X-ktb-news-editor-Source`` header below.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)

_TWITTER_RE = re.compile(
    r"^https?://(?:www\.)?(?:x|twitter)\.com/([^/\s]+)/status/(\d+)",
    re.IGNORECASE,
)

_TWITTER_HOSTS = {"x.com", "twitter.com", "www.x.com", "www.twitter.com"}
_GITHUB_HOSTS = {"github.com", "www.github.com"}


# ────────────────────────── domain → friendly name ──────────────────────────

DOMAIN_FRIENDLY_NAME: dict[str, str] = {
    "vnexpress.net":        "VnExpress",
    "thanhnien.vn":         "Thanh Niên",
    "tuoitre.vn":           "Tuổi Trẻ",
    "dantri.com.vn":        "Dân Trí",
    "vietnamnet.vn":        "VietnamNet",
    "genk.vn":              "GenK",
    "tinhte.vn":            "Tinh Tế",
    "kenh14.vn":            "Kenh14",
    "soha.vn":              "Soha",
    "cafef.vn":             "CafeF",
    "cafebiz.vn":           "CafeBiz",
    "techcrunch.com":       "TechCrunch",
    "theverge.com":         "The Verge",
    "wired.com":            "Wired",
    "github.com":           "GitHub",
    "gist.github.com":      "GitHub Gist",
    "huggingface.co":       "Hugging Face",
    "x.com":                "X (Twitter)",
    "twitter.com":          "X (Twitter)",
    "facebook.com":         "Facebook",
    "instagram.com":        "Instagram",
    "linkedin.com":         "LinkedIn",
    "youtube.com":          "YouTube",
    "youtu.be":             "YouTube",
    "tiktok.com":           "TikTok",
    "reddit.com":           "Reddit",
    "news.ycombinator.com": "Hacker News",
    "medium.com":           "Medium",
    "dev.to":               "DEV Community",
    "anthropic.com":        "Anthropic",
    "openai.com":           "OpenAI",
}


def friendly_source_name(domain: str) -> str:
    """Map a domain to a human-readable source name.

    Falls back to the title-cased first segment of the domain.
    """
    if not domain:
        return ""
    domain = domain.lower().lstrip(".")
    if domain.startswith("www."):
        domain = domain[4:]
    if domain in DOMAIN_FRIENDLY_NAME:
        return DOMAIN_FRIENDLY_NAME[domain]
    root = domain.split(".")[0]
    return root.capitalize() if root else domain


def extract_domain(url: str) -> str:
    """``'https://vnexpress.net/article/...'`` → ``'vnexpress.net'``."""
    try:
        parsed = urlparse(url)
        host = parsed.netloc or parsed.path
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return url


def extract_url_from_text(text: str) -> str | None:
    """Find the first http(s) URL inside arbitrary text.

    Strips trailing punctuation that wouldn't be part of the URL.
    """
    if not text:
        return None
    m = _URL_INLINE_RE.search(text)
    if not m:
        return None
    return m.group(0).rstrip(".,;:!?)")


# ────────────────────────── URL fetch ──────────────────────────

@dataclass
class FetchResult:
    """Structured result of an article fetch, replacing the previous module-global
    stash used to smuggle og:image alongside the text return."""
    text: str
    og_image: str | None = None
    article_images: list[str] = field(default_factory=list)


def _extract_twitter_info(url: str) -> dict | None:
    m = _TWITTER_RE.match(url)
    if m:
        return {"twitter_username": m.group(1), "tweet_id": m.group(2)}
    return None


def _has_heading(text: str) -> bool:
    """True if any line (after lstrip) starts with ``#``."""
    return any(line.lstrip().startswith("#") for line in text.splitlines())


def _normalize_markdown(md: str) -> str:
    """Strip markdown but preserve structure.

    - ``# Heading``        → ``HEADING`` (uppercased plain line)
    - ``- item`` / ``* x`` → ``- item``
    - everything else      → verbatim
    """
    heading_re = re.compile(r"^(#{1,6})\s+(.*)$")
    bullet_re = re.compile(r"^\s*[-*]\s+(.*)$")

    out: list[str] = []
    for line in md.splitlines():
        h = heading_re.match(line)
        if h:
            out.append(h.group(2).strip().upper())
            continue
        b = bullet_re.match(line)
        if b:
            out.append(f"- {b.group(1).strip()}")
            continue
        out.append(line)
    return "\n".join(out).strip()


def _attr_str(tag, name: str) -> str:
    """Read a BS4 attribute as a plain string.

    BS4 returns ``str | AttributeValueList | None`` for ``tag.get(name)``
    because some HTML attributes (``class``, ``rel``, ``srcset``) are
    multi-valued. Coerce to a single string so the rest of the code can
    treat it like text without type-narrowing dances.
    """
    if tag is None:
        return ""
    v = tag.get(name)
    if v is None:
        return ""
    if isinstance(v, list):
        return " ".join(v) if v else ""
    return str(v)


def _extract_og_image_from_soup(soup: BeautifulSoup, base_url: str = "") -> str | None:
    """Find a representative image — og:image → twitter:image → first big <img>."""

    def _abs(u: str) -> str:
        return urljoin(base_url, u) if base_url and u else u

    for prop in ("og:image", "og:image:url", "og:image:secure_url"):
        m = soup.find("meta", attrs={"property": prop})
        content = _attr_str(m, "content")
        if content:
            return _abs(content.strip())
    for name in ("twitter:image", "twitter:image:src"):
        m = soup.find("meta", attrs={"name": name})
        content = _attr_str(m, "content")
        if content:
            return _abs(content.strip())
    art = soup.find("article") or soup.find("main") or soup.body
    if art:
        for img in art.find_all("img"):
            src = _attr_str(img, "src") or _attr_str(img, "data-src")
            if not src:
                continue
            try:
                w = int(_attr_str(img, "width") or 0)
                h = int(_attr_str(img, "height") or 0)
            except ValueError:
                w = h = 0
            if 0 < w < 300 and 0 < h < 300:
                continue
            if any(kw in src.lower() for kw in ("logo", "icon", "favicon", "pixel.gif", "tracker")):
                continue
            return _abs(src.strip())
    return None


def _extract_article_images_from_soup(
    soup: BeautifulSoup,
    base_url: str = "",
    exclude_url: str = "",
    max_n: int = 5,
) -> list[str]:
    """Extract up to ``max_n`` real content <img> URLs from <article>/<main>.

    Skips tracking pixels, logos, avatars, ads. Deduplicates against
    ``exclude_url`` (typically the og:image already captured separately).
    """

    def _abs(u: str) -> str:
        return urljoin(base_url, u) if base_url else u

    art = soup.find("article") or soup.find("main") or soup.body
    if art is None:
        return []

    seen: set[str] = set()
    if exclude_url:
        seen.add(exclude_url)
    out: list[str] = []

    skip_keywords = ("logo", "icon", "favicon", "pixel.gif", "tracker",
                     "avatar", "/ads/", "sprite", "emoji", "1x1.")

    for img in art.find_all("img"):
        if len(out) >= max_n:
            break
        src = (
            _attr_str(img, "data-src")
            or _attr_str(img, "data-original")
            or _attr_str(img, "data-lazy-src")
            or _attr_str(img, "src")
        )
        if not src:
            srcset = _attr_str(img, "srcset")
            if srcset:
                parts = [p.strip().split() for p in srcset.split(",") if p.strip()]
                if parts:
                    src = parts[-1][0] if parts[-1] else ""
        if not src:
            continue
        lower = src.lower()
        if any(kw in lower for kw in skip_keywords):
            continue
        try:
            w = int(_attr_str(img, "width") or 0)
            h = int(_attr_str(img, "height") or 0)
        except ValueError:
            w = h = 0
        if 0 < w < 200 or 0 < h < 200:
            continue
        absu = _abs(src.strip())
        if not absu.startswith(("http://", "https://")):
            continue
        if absu in seen:
            continue
        seen.add(absu)
        out.append(absu)
    return out


async def fetch_url(url: str) -> FetchResult:
    """Fetch a URL and extract readable article text + key images.

    Uses ``httpx.AsyncClient(follow_redirects=True, timeout=20.0)``. Strips
    ``script``/``style``/``nav``/``footer``/``header``/``aside``/``noscript``
    via BeautifulSoup, prefers ``<article>`` then ``<main>`` then ``<body>``,
    collapses excessive blank lines, and caps the text at 8000 chars.
    """
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        "X-ktb-news-editor-Source": "github.com/ktbteam/ktb-studio",
    }
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=_FETCH_TIMEOUT,
        headers=headers,
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        html = resp.text

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    og_image = _extract_og_image_from_soup(soup, base_url=url)
    article_images = _extract_article_images_from_soup(
        soup, base_url=url, exclude_url=og_image or "", max_n=5,
    )

    for tag in soup(_STRIP_TAGS):
        tag.decompose()

    root = soup.find("article") or soup.find("main") or soup.body
    text = (root or soup).get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return FetchResult(
        text=text[:_MAX_CHARS],
        og_image=og_image,
        article_images=article_images,
    )


# ────────────────────────── public entry point ──────────────────────────


async def detect_and_normalize(raw_input: str) -> tuple[SourceType, str, dict]:
    """Detect input type and normalize.

    Returns ``(source_type, text, meta)``. ``meta`` carries optional keys
    ``source_url``, ``source_domain``, ``source_image``, ``source_images``,
    ``github_stats``, and (for X/Twitter) ``twitter_username`` / ``tweet_id``.
    """
    if not raw_input:
        return "text", "", {}

    stripped = raw_input.strip()
    if not stripped:
        return "text", "", {}

    if _URL_RE.match(stripped):
        return await _normalize_url(stripped)

    if "\n" in stripped and _has_heading(stripped):
        return "markdown", _normalize_markdown(stripped), {}

    return "text", stripped, {}


async def _normalize_url(url: str) -> tuple[SourceType, str, dict]:
    domain = extract_domain(url)
    meta: dict = {"source_url": url, "source_domain": domain}

    # X/Twitter: HTML is login-walled — skip fetch, emit a stub for the planner
    # to enrich via Google Search grounding.
    if domain in _TWITTER_HOSTS:
        twitter_info = _extract_twitter_info(url)
        if twitter_info:
            meta.update(twitter_info)
            stub = (
                f"Tweet by @{twitter_info['twitter_username']} "
                f"(tweet ID: {twitter_info['tweet_id']}).\n"
                f"Tweet content is NOT directly accessible — login wall blocks scraping.\n"
                f"Use Google Search to find:\n"
                f"  - What @{twitter_info['twitter_username']} is famous for\n"
                f"  - Recent activity of this account (last 30 days)\n"
                f"  - Likely content of tweet {twitter_info['tweet_id']} (if indexed)\n"
                f"  - Related news/discussion around this account"
            )
            return "url", stub, meta
        # Couldn't extract — fall through to normal fetch (will likely 401/login).

    try:
        result = await fetch_url(url)
    except httpx.HTTPError as exc:
        logger.warning("fetch_url failed for %s: %s", url, exc)
        return "url", "", meta

    text = result.text
    if result.og_image:
        meta["source_image"] = result.og_image
    if result.article_images:
        meta["source_images"] = list(result.article_images)

    if domain in _GITHUB_HOSTS:
        github_stats = await fetch_github_stats(url)
        if github_stats:
            meta["github_stats"] = github_stats
            text = (text or "") + "\n\n" + format_github_stats_block(github_stats)

    return "url", text, meta


__all__ = [
    "SourceType",
    "FetchResult",
    "DOMAIN_FRIENDLY_NAME",
    "friendly_source_name",
    "extract_domain",
    "extract_url_from_text",
    "fetch_url",
    "detect_and_normalize",
]
