"""Multi-source aggregator — fetch N article URLs in parallel and merge.

Used by :mod:`tools.briefa` to feed the planner a *neutral, multi-
perspective* source built from several news outlets covering the same
event. The planner then has explicit attribution markers to draw from, so
the rendered video can quote each source by name without invention.

Output text shape::

    ═══════════════════════════════════════════════════════════════
    MULTI_SOURCE_DOSSIER — 3 source(s) on the same event.
    Use these as independent perspectives; cite each by name in the video.
    ═══════════════════════════════════════════════════════════════

    [SOURCE 1] VnExpress (vnexpress.net)
    URL: https://vnexpress.net/...
    ---
    <article text 1>

    [SOURCE 2] Thanh Niên (thanhnien.vn)
    ...

The meta dict contains:

  - ``source_kind``       — always ``"multi_url"``
  - ``source_urls``       — list of URLs in input order
  - ``source_domains``    — list of domains in input order
  - ``source_names``      — list of friendly names in input order
  - ``primary_source_url``    — alias of the first URL (for AINews compat)
  - ``primary_source_domain`` — alias of the first domain
  - ``source_image``      — og:image of the first successful fetch (if any)
  - ``source_images``     — flat list of inline images across all sources
                            (up to ``per_source_image_limit`` each)
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

from .url_extract import (
    FetchResult,
    extract_domain,
    fetch_url,
    friendly_source_name,
)

logger = logging.getLogger("briefa.sources.multi_source")


@dataclass
class SourceFetch:
    """Outcome of fetching a single URL inside a multi-source request."""
    url: str
    domain: str
    friendly_name: str
    text: str
    og_image: str | None = None
    article_images: list[str] | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.error


async def _fetch_one(url: str, timeout: float) -> SourceFetch:
    domain = extract_domain(url)
    name = friendly_source_name(domain) or domain
    try:
        result: FetchResult = await asyncio.wait_for(fetch_url(url), timeout=timeout)
    except TimeoutError:
        logger.warning("multi_source: %s timed out after %.1fs", url, timeout)
        return SourceFetch(url=url, domain=domain, friendly_name=name, text="", error="timeout")
    except httpx.HTTPError as exc:
        # ConnectTimeout / ConnectError carry empty str(exc) — fall back to the
        # class name so the user sees *why* the fetch failed instead of a blank.
        err = str(exc) or type(exc).__name__
        logger.warning("multi_source: %s fetch failed: %s", url, err)
        return SourceFetch(url=url, domain=domain, friendly_name=name, text="", error=err)
    except Exception as exc:
        err = str(exc) or type(exc).__name__
        logger.warning("multi_source: %s unexpected fetch error: %s", url, err)
        return SourceFetch(url=url, domain=domain, friendly_name=name, text="", error=err)
    return SourceFetch(
        url=url,
        domain=domain,
        friendly_name=name,
        text=result.text,
        og_image=result.og_image,
        article_images=list(result.article_images or []),
    )


def _merge_text(sources: list[SourceFetch]) -> str:
    """Render successful fetches as one labelled dossier."""
    ok = [s for s in sources if s.ok]
    if not ok:
        return ""
    header = (
        "═══════════════════════════════════════════════════════════════\n"
        f"MULTI_SOURCE_DOSSIER — {len(ok)} source(s) on the same event.\n"
        "Use these as independent perspectives; cite each by name in the video.\n"
        "═══════════════════════════════════════════════════════════════\n"
    )
    blocks: list[str] = [header]
    for i, src in enumerate(ok, start=1):
        blocks.append(
            f"\n[SOURCE {i}] {src.friendly_name} ({src.domain})\n"
            f"URL: {src.url}\n"
            "---\n"
            f"{src.text.strip()}\n"
        )
    return "".join(blocks)


def _merge_meta(
    sources: list[SourceFetch],
    *,
    per_source_image_limit: int,
) -> dict:
    """Aggregate per-source metadata into the shape AINews / composer expect."""
    ok = [s for s in sources if s.ok]
    meta: dict = {
        "source_kind": "multi_url",
        "source_urls": [s.url for s in sources],
        "source_domains": [s.domain for s in sources],
        "source_names": [s.friendly_name for s in sources],
    }
    if ok:
        meta["primary_source_url"] = ok[0].url
        meta["primary_source_domain"] = ok[0].domain
        # AINews / composer look at the canonical "source_url" / "source_domain"
        # / "source_image" / "source_images" keys — keep those populated by the
        # primary source so existing code paths still light up.
        meta["source_url"] = ok[0].url
        meta["source_domain"] = ok[0].domain
        first_with_og = next((s for s in ok if s.og_image), None)
        if first_with_og is not None:
            meta["source_image"] = first_with_og.og_image

    flat_images: list[str] = []
    seen: set[str] = set()
    for src in ok:
        for url in (src.article_images or [])[:per_source_image_limit]:
            if url in seen:
                continue
            seen.add(url)
            flat_images.append(url)
    if flat_images:
        meta["source_images"] = flat_images
    return meta


async def fetch_multi(
    urls: list[str],
    *,
    fetch_timeout: float = 25.0,
    per_source_image_limit: int = 2,
) -> tuple[str, dict, list[SourceFetch]]:
    """Fetch every URL concurrently and merge into a single dossier.

    Args:
        urls:                    URLs to fetch. Duplicates are removed in input
                                 order. Order is preserved in the output.
        fetch_timeout:           Per-URL hard timeout. A timed-out source is
                                 surfaced as a :class:`SourceFetch` with
                                 ``error="timeout"`` and skipped in the dossier.
        per_source_image_limit:  Max inline images kept per source before
                                 deduping into ``meta["source_images"]``.

    Returns:
        ``(dossier_text, meta, per_source_results)``.

    Raises:
        ValueError: when no URLs are provided.
    """
    if not urls:
        raise ValueError("fetch_multi() needs at least one URL")

    seen: set[str] = set()
    deduped: list[str] = []
    for u in urls:
        u = u.strip()
        if not u or u in seen:
            continue
        seen.add(u)
        deduped.append(u)

    sources = await asyncio.gather(*(_fetch_one(u, fetch_timeout) for u in deduped))
    text = _merge_text(list(sources))
    meta = _merge_meta(list(sources), per_source_image_limit=per_source_image_limit)
    return text, meta, list(sources)


__all__ = ["SourceFetch", "fetch_multi"]
