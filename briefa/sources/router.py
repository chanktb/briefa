"""Unified input router for Briefa N-mix-input mode.

A Briefa request can carry any combination of inputs:

  * plain text / markdown ("user notes")
  * URL pointing at a news article (auto-fetch + extract)
  * URL pointing at a public GitHub repo (stats + README + recent commits)
  * local image path (Gemini Vision OCR)

Each input is normalized into a :class:`BriefaInput` carrying enough metadata
for the composer to bind a CITATION CHIP to every scene that quotes it. The
planner sees a merged dossier with explicit ``[SOURCE n]`` markers so it can
emit a ``citation_source_index`` per scene.

Public entry point: :func:`route_and_merge`.

This module is the only piece in ``briefa/sources/`` that didn't ship with the
ktb-ai-news fork — every other fetcher is reused as-is. See ``DECISIONS.md``
D005 for the fork rationale.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .github import fetch_repo_full, format_repo_full_block, parse_repo_url
from .url_extract import (
    extract_domain,
    extract_url_from_text,
    fetch_url,
    friendly_source_name,
)
from .vision import extract_from_images

logger = logging.getLogger("briefa.sources.router")

InputKind = Literal["text", "article", "github_repo", "image"]


# ════════════════════════════════════════════════════════════════════════
# Data shapes
# ════════════════════════════════════════════════════════════════════════


@dataclass
class CitationChip:
    """Display data for the per-scene citation chip rendered by the composer."""

    name: str            # friendly source name, e.g. "VnExpress" or "GitHub: owner/repo"
    domain: str = ""     # e.g. "vnexpress.net" — used to pick a favicon
    url: str = ""        # canonical source URL (empty for plain text / image)

    def as_dict(self) -> dict:
        return {"name": self.name, "domain": self.domain, "url": self.url}


@dataclass
class BriefaInput:
    """A single ingested input after routing.

    The planner consumes the ``text`` blob (joined into the dossier by
    :func:`route_and_merge`); the composer consumes ``citation`` to bind
    each scene to a chip.
    """

    source_id: str                      # e.g. "src_001"
    kind: InputKind
    raw: str                            # original CLI arg (URL / text / path)
    text: str                           # normalized text for the planner
    citation: CitationChip
    meta: dict = field(default_factory=dict)
    ok: bool = True
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "kind": self.kind,
            "raw": self.raw,
            "citation": self.citation.as_dict(),
            "meta": self.meta,
            "ok": self.ok,
            "error": self.error,
        }


# ════════════════════════════════════════════════════════════════════════
# Detection
# ════════════════════════════════════════════════════════════════════════


def detect_kind(raw: str | Path) -> InputKind:
    """Pick the routing branch for one raw input.

    Resolution order:

    1. ``Path``-like that exists on disk → ``image``
    2. starts with http(s) AND parses as GitHub repo → ``github_repo``
    3. starts with http(s) → ``article``
    4. otherwise → ``text``
    """
    if isinstance(raw, Path):
        return "image"
    s = (raw or "").strip()
    if not s:
        return "text"
    # Explicit file paths beat URL detection (a local file named "http..." is
    # exotic enough to not bother with).
    p = Path(s)
    try:
        if p.exists() and p.is_file():
            return "image" if p.suffix.lower() in {
                ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp",
            } else "text"
    except OSError:
        pass
    if s.lower().startswith(("http://", "https://")):
        if parse_repo_url(s) is not None:
            return "github_repo"
        return "article"
    return "text"


# ════════════════════════════════════════════════════════════════════════
# Per-kind fetchers
# ════════════════════════════════════════════════════════════════════════


def _citation_from_override(override: str, fallback_url: str = "") -> CitationChip:
    """Build a CitationChip from a user-typed source override string.

    Heuristic: if the override looks like a URL/domain (contains a dot,
    no spaces), treat it as a domain and use the friendly name. Else
    treat as a free-form label (channel name, "Cộng đồng X", etc.).

    ``fallback_url`` is preserved when override is just a free-form
    name so the chip still has a link target if one was detected from
    the body text.
    """
    s = (override or "").strip()
    if not s:
        return CitationChip(name="")
    # URL-like: starts with http or contains scheme-less domain pattern.
    looks_url = s.lower().startswith(("http://", "https://"))
    looks_domain = ("." in s) and (" " not in s) and len(s) <= 80
    if looks_url:
        domain = extract_domain(s)
        return CitationChip(
            name=friendly_source_name(domain) or domain or s,
            domain=domain,
            url=s,
        )
    if looks_domain:
        # e.g. "vnexpress.net" → name="VnExpress", domain="vnexpress.net"
        domain = s.lower()
        return CitationChip(
            name=friendly_source_name(domain) or domain,
            domain=domain,
            url=fallback_url,
        )
    # Free-form label
    return CitationChip(name=s, domain="", url=fallback_url)


async def _route_text(
    raw: str,
    source_id: str,
    label_hint: str = "",
    source_override: str = "",
) -> BriefaInput:
    """Pass-through; surface embedded URL (if any) as citation.

    ``source_override`` (CEO 2026-06-17): when user explicitly types a
    source label per input on the frontend, override the default
    "User notes" / embedded-URL detection so the chip reads what the
    user typed (e.g. "VnExpress", "Tuoi Tre Online", "tuoitre.vn").
    """
    text = raw.strip()
    citation = CitationChip(name=label_hint or "User notes")
    embedded = extract_url_from_text(text)
    if embedded:
        citation = CitationChip(
            name=friendly_source_name(extract_domain(embedded)) or "Embedded link",
            domain=extract_domain(embedded),
            url=embedded,
        )
    if source_override.strip():
        citation = _citation_from_override(source_override.strip(), fallback_url=embedded or "")
    meta = {"original_text": text}
    if embedded:
        meta["embedded_url"] = embedded
    return BriefaInput(
        source_id=source_id,
        kind="text",
        raw=raw,
        text=text,
        citation=citation,
        meta=meta,
    )


async def _route_article(
    url: str,
    source_id: str,
    source_override: str = "",
) -> BriefaInput:
    """Fetch + extract an article URL."""
    domain = extract_domain(url)
    name = friendly_source_name(domain) or domain
    citation = CitationChip(name=name, domain=domain, url=url)
    if source_override.strip():
        citation = _citation_from_override(source_override.strip(), fallback_url=url)
    try:
        result = await fetch_url(url)
    except Exception as exc:  # noqa: BLE001 — surface any fetch failure
        err = str(exc) or type(exc).__name__
        logger.warning("router: article fetch failed for %s: %s", url, err)
        return BriefaInput(
            source_id=source_id,
            kind="article",
            raw=url,
            text="",
            citation=citation,
            meta={"source_url": url, "source_domain": domain},
            ok=False,
            error=err,
        )

    meta: dict = {
        "source_url": url,
        "source_domain": domain,
        "source_kind_router": "article",
    }
    if result.og_image:
        meta["source_image"] = result.og_image
    if result.article_images:
        meta["source_images"] = list(result.article_images)

    return BriefaInput(
        source_id=source_id,
        kind="article",
        raw=url,
        text=result.text or "",
        citation=citation,
        meta=meta,
        ok=bool(result.text),
        error="" if result.text else "empty_body",
    )


async def _route_github_repo(
    url: str,
    source_id: str,
    source_override: str = "",
) -> BriefaInput:
    """Fetch stats + README + commits for a public GitHub repo."""
    parsed = parse_repo_url(url) or ("?", "?")
    owner, repo = parsed
    full_name = f"{owner}/{repo}"
    citation = CitationChip(
        name=f"GitHub: {full_name}",
        domain="github.com",
        url=url,
    )
    if source_override.strip():
        citation = _citation_from_override(source_override.strip(), fallback_url=url)
    repo_full = await fetch_repo_full(url)
    if repo_full is None:
        logger.warning("router: GitHub fetch failed for %s", url)
        return BriefaInput(
            source_id=source_id,
            kind="github_repo",
            raw=url,
            text="",
            citation=citation,
            meta={"source_url": url, "source_domain": "github.com"},
            ok=False,
            error="github_api_failed",
        )

    stats = repo_full.get("stats") or {}
    block = format_repo_full_block(repo_full)
    meta = {
        "source_url": url,
        "source_domain": "github.com",
        "source_kind_router": "github_repo",
        "github_stats": stats,
        "github_readme_chars": len(repo_full.get("readme") or ""),
        "github_recent_commits": repo_full.get("commits") or [],
    }
    if stats.get("owner_avatar_url"):
        meta["source_image"] = stats["owner_avatar_url"]
    return BriefaInput(
        source_id=source_id,
        kind="github_repo",
        raw=url,
        text=block,
        citation=citation,
        meta=meta,
    )


async def _route_image(
    path: Path,
    source_id: str,
    hint: str = "",
    source_override: str = "",
) -> BriefaInput:
    """Vision OCR a screenshot. Hint is added as extra context to Gemini."""
    citation = CitationChip(name=f"Screenshot: {path.name}")
    if source_override.strip():
        citation = _citation_from_override(source_override.strip())
    try:
        extracted = await extract_from_images([path], hint=hint)
    except Exception as exc:  # noqa: BLE001
        err = str(exc) or type(exc).__name__
        logger.warning("router: vision OCR failed for %s: %s", path, err)
        return BriefaInput(
            source_id=source_id,
            kind="image",
            raw=str(path),
            text="",
            citation=citation,
            meta={"image_path": str(path)},
            ok=False,
            error=err,
        )
    return BriefaInput(
        source_id=source_id,
        kind="image",
        raw=str(path),
        text=extracted or "",
        citation=citation,
        meta={"image_path": str(path), "image_hint": hint},
        ok=bool(extracted),
        error="" if extracted else "vision_empty",
    )


# ════════════════════════════════════════════════════════════════════════
# Public API
# ════════════════════════════════════════════════════════════════════════


async def route_inputs(
    inputs: list[str | Path],
    *,
    image_hint: str = "",
    source_overrides: list[str | None] | None = None,
) -> list[BriefaInput]:
    """Detect + fetch every input concurrently.

    Returns the per-input results in the SAME ORDER the caller passed them,
    so ``source_id`` aligns with the user-visible order on screen.

    ``source_overrides`` (CEO 2026-06-17): optional parallel list (same
    length + order as ``inputs``); a non-empty string at index i tells the
    router to use that string for the citation chip on input i instead of
    auto-deriving from kind/domain. Empty/None entries keep default
    behavior. Index mismatches are tolerated (missing → default).
    """
    if not inputs:
        return []

    overrides = list(source_overrides or [])

    def _override_for(idx0: int) -> str:
        if idx0 >= len(overrides):
            return ""
        v = overrides[idx0]
        return (v or "").strip()

    coros: list = []
    for idx, raw in enumerate(inputs, start=1):
        source_id = f"src_{idx:03d}"
        kind = detect_kind(raw)
        ov = _override_for(idx - 1)
        if kind == "text":
            coros.append(_route_text(str(raw), source_id, source_override=ov))
        elif kind == "article":
            coros.append(_route_article(str(raw), source_id, source_override=ov))
        elif kind == "github_repo":
            coros.append(_route_github_repo(str(raw), source_id, source_override=ov))
        elif kind == "image":
            coros.append(_route_image(Path(raw), source_id, hint=image_hint, source_override=ov))
        else:  # pragma: no cover — exhaustive
            raise RuntimeError(f"unknown InputKind: {kind!r}")

    results = await asyncio.gather(*coros)
    return list(results)


def merge_to_dossier(sources: list[BriefaInput]) -> tuple[str, dict]:
    """Render successful sources as one labelled dossier for the planner.

    The dossier preserves source order and uses ``[SOURCE n]`` markers so the
    planner can emit ``citation_source_index`` per scene pointing at the
    correct entry. Failed sources are summarised at the bottom as a
    ``FETCH_FAILURES`` block so Gemini knows what was attempted but missing.

    BUG fix 2026-06-14: the dossier instruction header used to leak into
    ``voice_script`` and into the iPhone reader's ``article_body`` because
    pipeline.py was setting ``meta["original_text"] = dossier``. The leak
    surfaced as a video where the TTS reads "BRIEFA_MULTI_SOURCE — 1
    ingested source(s)..." aloud.

    Fix: this function now also returns ``meta["clean_text"]`` — the raw
    source content concatenated WITHOUT any header, ``[SOURCE n]`` markers,
    or instruction prose. Callers (pipeline.py, composer.py) MUST use
    ``clean_text`` for the iPhone reader / voice-intro extraction, and
    keep the full ``dossier`` only as the user-message payload for Gemini.
    """
    ok = [s for s in sources if s.ok and s.text.strip()]
    failed = [s for s in sources if not s.ok or not s.text.strip()]

    blocks: list[str] = []
    header = (
        "═══════════════════════════════════════════════════════════════\n"
        f"BRIEFA_MULTI_SOURCE — {len(ok)} ingested source(s).\n"
        "Each scene in the output MUST cite ONE source by its [SOURCE n] index.\n"
        "Set citation_source_index = n (1-based). DO NOT mix facts across sources\n"
        "without making the attribution explicit in voice_script.\n"
        "═══════════════════════════════════════════════════════════════\n"
    )
    blocks.append(header)

    # Clean concatenated source contents — for iPhone reader + voice-intro
    # fallback. Joined with paragraph breaks, no [SOURCE n] / ═══ markers.
    clean_parts: list[str] = []

    for idx, src in enumerate(ok, start=1):
        kind_label = {
            "article":     "ARTICLE",
            "github_repo": "GITHUB REPO",
            "text":        "USER TEXT",
            "image":       "SCREENSHOT",
        }.get(src.kind, src.kind.upper())
        head = (
            f"\n[SOURCE {idx}] {kind_label} · {src.citation.name}"
            f"{f' ({src.citation.domain})' if src.citation.domain else ''}\n"
        )
        if src.citation.url:
            head += f"URL: {src.citation.url}\n"
        head += "---\n"
        blocks.append(head + src.text.strip() + "\n")

        # Pure content for clean_text — NO markers.
        clean_parts.append(src.text.strip())

    if failed:
        blocks.append(
            "\n═══════════════════════════════════════════════════════════════\n"
            f"FETCH_FAILURES — {len(failed)} source(s) could not be ingested:\n"
            "═══════════════════════════════════════════════════════════════\n"
        )
        for src in failed:
            blocks.append(f"- ({src.kind}) {src.raw} — {src.error or 'empty'}\n")
        blocks.append(
            "Do not invent content for these. If a key fact would have come from\n"
            "one of them, write 'không đủ thông tin' in the relevant scene.\n"
        )

    dossier = "".join(blocks).strip() + "\n"
    clean_text = "\n\n".join(clean_parts).strip()

    # Aggregated meta the composer reads.
    meta: dict = {
        "source_kind": "mix_multi" if len(ok) > 1 else (ok[0].kind if ok else "empty"),
        "num_sources_ok": len(ok),
        "num_sources_failed": len(failed),
        "sources": [s.as_dict() for s in sources],
        # Marker-free concatenation of all source content. Pipeline sets
        # meta["original_text"] = clean_text so the iPhone reader scene +
        # voice-intro fallback never see the instruction header.
        "clean_text": clean_text,
    }
    # Pre-pick a primary source for any composer code path that still expects
    # the canonical ktb-ai-news shape (single source_url / source_domain).
    if ok:
        primary = ok[0]
        meta["primary_source_id"] = primary.source_id
        if primary.citation.url:
            meta["source_url"] = primary.citation.url
        if primary.citation.domain:
            meta["source_domain"] = primary.citation.domain
        if primary.meta.get("source_image"):
            meta["source_image"] = primary.meta["source_image"]
    # Flatten inline images from every ok source so ScreenshotEmbed layouts
    # have a deeper pool to round-robin through.
    flat_images: list[str] = []
    seen: set[str] = set()
    for src in ok:
        for url in src.meta.get("source_images") or []:
            if url in seen:
                continue
            seen.add(url)
            flat_images.append(url)
    if flat_images:
        meta["source_images"] = flat_images

    return dossier, meta


async def route_and_merge(
    inputs: list[str | Path],
    *,
    image_hint: str = "",
    source_overrides: list[str | None] | None = None,
) -> tuple[str, dict, list[BriefaInput]]:
    """One-stop: detect → fetch concurrently → merge into a dossier.

    Returns ``(dossier_text, meta, per_input_results)``.
    """
    sources = await route_inputs(
        inputs,
        image_hint=image_hint,
        source_overrides=source_overrides,
    )
    dossier, meta = merge_to_dossier(sources)
    return dossier, meta, sources


__all__ = [
    "InputKind",
    "CitationChip",
    "BriefaInput",
    "detect_kind",
    "route_inputs",
    "merge_to_dossier",
    "route_and_merge",
]
