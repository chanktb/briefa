"""Pexels free-stock-photo client.

Used by VCM when ``image_source="stock"`` — the user picks "real photos"
instead of cartoon AI gen. Pexels is a great free tier (200 req/hour, 20k
req/month, no charges) — get a key at https://www.pexels.com/api/new/
and set ``PEXELS_API_KEY`` in the project ``.env``.

The client searches Pexels for a query, picks the highest-res photo
matching the orientation hint, downloads it, and saves it as a JPG so
the rest of the VCM pipeline (which expects local files) keeps working.

History: until v0.3 VCM only knew the AI providers (Cloudflare Workers
AI + Pollinations). Anh wanted "hình thật" too — Pexels is the cleanest
free path: real photos, no AI artifacts, no licence headaches.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path
from typing import Literal

import httpx

logger = logging.getLogger("briefa.images.stock")

PEXELS_SEARCH_URL = "https://api.pexels.com/v1/search"
HTTP_TIMEOUT = 30.0
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.5

Orientation = Literal["portrait", "landscape", "square"]


def resolve_credentials() -> str | None:
    """Return the Pexels API key from env, or ``None`` when unset.

    ``None`` is a soft signal — :mod:`image_provider` treats it as "skip
    Pexels, try the next provider".
    """
    key = (os.environ.get("PEXELS_API_KEY") or "").strip()
    return key or None


def _orientation_for_dims(width: int, height: int) -> Orientation:
    if height > width * 1.15:
        return "portrait"
    if width > height * 1.15:
        return "landscape"
    return "square"


def _pick_best_photo(photos: list[dict], orientation: Orientation) -> dict | None:
    """Return the highest-quality photo url for the requested orientation.

    Pexels each ``photo`` dict has ``src`` with multiple sizes:
        original, large2x, large, medium, small, portrait, landscape, tiny
    We pick ``portrait``/``landscape`` directly when available, else
    ``large2x`` then ``large``.
    """
    if not photos:
        return None
    # The API filters by orientation but still returns square-ish results
    # sometimes. Prefer the first match — Pexels ranks by relevance already.
    photo = photos[0]
    src = photo.get("src", {})
    url = (
        src.get(orientation)
        or src.get("large2x")
        or src.get("large")
        or src.get("medium")
        or src.get("original")
    )
    if not url:
        return None
    return {"url": url, "photographer": photo.get("photographer", "")}


async def search_and_download(
    query: str,
    out_path: Path,
    *,
    api_key: str,
    width: int = 1080,
    height: int = 1920,
    per_page: int = 5,
) -> Path:
    """Search Pexels for ``query`` and save the best photo to ``out_path``.

    Returns ``out_path`` on success.

    Raises:
        RuntimeError: when all retries fail. Callers should catch and fall
            through to the next provider (CF / Pollinations / placeholder).
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    orientation = _orientation_for_dims(width, height)

    params = {
        "query": query.strip(),
        "orientation": orientation,
        "per_page": per_page,
        "size": "large",  # the highest-quality bucket
    }
    headers = {"Authorization": api_key}

    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                resp = await client.get(PEXELS_SEARCH_URL, headers=headers, params=params)

            if resp.status_code == 200:
                data = resp.json()
                photos = data.get("photos") or []
                pick = _pick_best_photo(photos, orientation)
                if pick is None:
                    raise RuntimeError(
                        f"Pexels: no photos for {query!r} (orientation={orientation})"
                    )
                # Download the actual JPG.
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                    img_resp = await client.get(pick["url"])
                if img_resp.status_code != 200 or not img_resp.content:
                    raise RuntimeError(
                        f"Pexels: photo download HTTP {img_resp.status_code}"
                    )
                out_path.write_bytes(img_resp.content)
                logger.info(
                    "pexels saved %s (%d KB, query=%r, photographer=%s)",
                    out_path.name,
                    len(img_resp.content) // 1024,
                    query,
                    pick["photographer"],
                )
                return out_path

            last_err = RuntimeError(
                f"Pexels HTTP {resp.status_code}: {resp.text[:300]}"
            )
            logger.warning("pexels attempt %d/%d -> %s", attempt, MAX_RETRIES, last_err)
            # 401 = bad key, 403 = quota exhausted — no point retrying.
            if resp.status_code in (401, 403, 429):
                break
        except (httpx.HTTPError, httpx.ReadTimeout) as exc:
            last_err = exc
            logger.warning(
                "pexels attempt %d/%d failed: %s", attempt, MAX_RETRIES, exc,
            )
        if attempt < MAX_RETRIES:
            await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)
        # Clean up any partial write.
        with contextlib.suppress(OSError):
            if out_path.is_file() and out_path.stat().st_size < 2048:
                out_path.unlink()

    raise RuntimeError(f"Pexels failed after {MAX_RETRIES} attempts: {last_err}")


__all__ = [
    "PEXELS_SEARCH_URL",
    "resolve_credentials",
    "search_and_download",
]
