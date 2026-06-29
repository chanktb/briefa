"""Image-provider router — tries every configured generator in order.

Tools should import :func:`generate_batch` from here rather than calling the
per-provider clients directly. The router picks the best available chain at
runtime based on env credentials AND the caller's ``image_source`` hint::

    image_source="ai"    (default, cartoon AI illustrations)
        1. Cloudflare Workers AI  if CF_ACCOUNT_ID + CF_AI_TOKEN
        2. Pollinations.ai        always tried
        3. Placeholder gradient   never raises

    image_source="stock" (real free stock photos)
        1. Pexels                 if PEXELS_API_KEY
        2. Cloudflare Workers AI  fallback when stock fails or no key
        3. Pollinations.ai        next fallback
        4. Placeholder gradient

When Cloudflare is configured it gets concurrency 4 (CF Workers AI tolerates
small bursts well). Pollinations fallback drops to ``max_concurrent=1`` to
respect the free endpoint's tight tolerance. Pexels gets concurrency 3
(well within its 200/hour quota even on bursty 20-scene long-form runs).

History:
  - Pollinations was the sole provider until they paywalled flux / turbo /
    flux-realism in early June 2026, which left VCM rendering with
    placeholder gradients only.
  - Restoring the original KhuePrinter pattern (CF primary, Pollinations
    fallback) fixed that — CF is still very free.
  - v0.3 (2026-06-04): added ``image_source="stock"`` for the Pexels real-
    photo path so anh can pick "ảnh thật" instead of cartoon AI gen.
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from . import cloudflare as cf
from . import pollinations as poll
from . import stock as stock_mod

logger = logging.getLogger("briefa.images.provider")


# Filler words to strip from an AI-style cartoon prompt before using it
# as a Pexels stock-photo search query. Pexels search matches keywords —
# a 30-word prompt like "cute cartoon illustration of a young astronomer
# looking through a telescope at a swirling black void..." returns 0
# results, but "astronomer telescope night sky" returns hundreds.
_STOCK_QUERY_STRIP = re.compile(
    r"\b("
    r"cartoon|cute|stylized|illustration|drawing|sketch|painting|"
    r"young|modern|colorful|colourful|vibrant|soft|warm|cool|"
    r"of|a|an|the|with|and|at|in|on|by|to|from|for|"
    r"sfw|family|friendly|modest|fully|clothed|"
    r"style|background|scene|setting|aesthetic|"
    r"looking|displaying|holding|standing|sitting|wearing"
    r")\b",
    re.IGNORECASE,
)


def _to_stock_query(prompt: str, max_words: int = 5) -> str:
    """Compress an AI cartoon prompt into a Pexels keyword search query.

    Strips filler words + leading style boilerplate, keeps the first
    ``max_words`` content nouns/adjectives. Good enough for graceful
    fallback when the AI providers are out of quota.
    """
    cleaned = _STOCK_QUERY_STRIP.sub(" ", prompt or "")
    # Drop punctuation + collapse whitespace.
    cleaned = re.sub(r"[^\w\s]+", " ", cleaned)
    words = [w for w in cleaned.split() if len(w) > 2]
    return " ".join(words[:max_words]).strip() or (prompt or "image")[:40]


async def generate_batch(
    prompts: list[str],
    out_dir: Path,
    *,
    image_source: str = "ai",
    style_suffix: str = poll.DEFAULT_STYLE_SUFFIX,
    width: int = poll.DEFAULT_WIDTH,
    height: int = poll.DEFAULT_HEIGHT,
    filename_prefix: str = "scene",
    cf_max_concurrent: int = 4,
    pollinations_max_concurrent: int = 1,
    stock_max_concurrent: int = 3,
) -> list[Path]:
    """Generate one image per prompt across the provider chain.

    The return list always has ``len(prompts)`` entries — every prompt that
    fails every provider falls through to a placeholder gradient written by
    :func:`core.images.pollinations.write_placeholder`.

    Args:
        prompts:              Free-text scene descriptions.
        out_dir:              Destination directory (created if missing).
        style_suffix:         Appended to every prompt for consistent look.
        width:                Per-image width hint (CF generates square,
                              Pollinations honours width × height).
        height:               Per-image height hint.
        filename_prefix:      File stem prefix. Final names = ``<prefix>_<n>.jpg``.
        cf_max_concurrent:    Concurrency cap for the Cloudflare leg.
        pollinations_max_concurrent: Concurrency cap for the Pollinations leg.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    cf_creds = cf.resolve_credentials()
    pexels_key = stock_mod.resolve_credentials()

    if image_source == "stock":
        if pexels_key is None:
            logger.info(
                "image provider: image_source=stock but PEXELS_API_KEY unset — "
                "falling back to AI chain (CF → Pollinations)"
            )
        else:
            logger.info(
                "image provider: image_source=stock — Pexels primary, "
                "CF + Pollinations fallback"
            )
    elif cf_creds is None:
        logger.info(
            "image provider: CF_ACCOUNT_ID / CF_AI_TOKEN unset — Pollinations only"
        )
    else:
        logger.info(
            "image provider: Cloudflare Workers AI primary (model=%s), "
            "Pollinations fallback",
            cf.DEFAULT_MODEL,
        )

    # Per-provider concurrency semaphores. Each prompt walks the chain
    # serially; across prompts the chain is fully parallel up to each
    # provider's slot count.
    cf_sem = asyncio.Semaphore(max(1, cf_max_concurrent))
    poll_sem = asyncio.Semaphore(max(1, pollinations_max_concurrent))
    stock_sem = asyncio.Semaphore(max(1, stock_max_concurrent))

    async def _try_stock(
        prompt: str, out_path: Path, idx: int, *, query_override: str | None = None,
    ) -> Path | None:
        if pexels_key is None:
            return None
        # When the caller picked "stock" the prompt is already a 2-4 word
        # keyword query from the planner. When we're falling back from a
        # failed AI run, the prompt is a 30-word cartoon description that
        # Pexels can't match — strip it down to keywords first.
        query = query_override if query_override is not None else prompt
        async with stock_sem:
            try:
                return await stock_mod.search_and_download(
                    query,
                    out_path,
                    api_key=pexels_key,
                    width=width,
                    height=height,
                )
            except Exception as exc:
                logger.info(
                    "image provider: Pexels failed for prompt %d (query=%r): %s",
                    idx + 1, query[:60], exc,
                )
                return None

    async def _try_cf(prompt: str, out_path: Path, idx: int) -> Path | None:
        if cf_creds is None:
            return None
        # Don't pin a single (account_id, api_token) here — let
        # cf.generate_image walk through ALL configured CF accounts on
        # 429 (one account's daily 10 k Neuron quota burning out
        # shouldn't immediately collapse to Pollinations/Pexels when
        # another account still has headroom).
        async with cf_sem:
            try:
                return await cf.generate_image(
                    prompt,
                    out_path,
                    seed=(idx + 1) * 100,
                    style_suffix=style_suffix,
                )
            except Exception as exc:
                logger.info(
                    "image provider: CF failed for prompt %d (%s) — trying Pollinations",
                    idx + 1, exc,
                )
                return None

    async def _try_pollinations(prompt: str, out_path: Path, idx: int) -> Path | None:
        async with poll_sem:
            chain: list[str] = []
            for m in [poll.DEFAULT_MODEL, *poll.DEFAULT_MODEL_CHAIN]:
                if m and m not in chain:
                    chain.append(m)
            last_err: Exception | None = None
            for candidate in chain:
                try:
                    return await poll.generate_image(
                        prompt,
                        out_path,
                        width=width,
                        height=height,
                        model=candidate,
                        seed=(idx + 1) * 100,
                        style_suffix=style_suffix,
                    )
                except Exception as exc:
                    last_err = exc
                    if "402" in str(exc) and candidate is not chain[-1]:
                        continue
                    break
            logger.info(
                "image provider: Pollinations failed for prompt %d (%s) — placeholder",
                idx + 1, last_err,
            )
            return None

    async def _one(idx: int, prompt: str) -> Path:
        out_path = out_dir / f"{filename_prefix}_{idx + 1}.jpg"
        # Stock chain first when caller asked for real photos.
        if image_source == "stock":
            result = await _try_stock(prompt, out_path, idx)
            if result is not None:
                return result
        # AI chain: Cloudflare → Pollinations.
        result = await _try_cf(prompt, out_path, idx)
        if result is not None:
            return result
        result = await _try_pollinations(prompt, out_path, idx)
        if result is not None:
            return result
        # AI mode + both AI providers failed (CF quota exhausted /
        # Pollinations paywalled) — try Pexels with keywords extracted
        # from the cartoon prompt before giving up to a gradient. Real
        # stock photos read MUCH better than a flat placeholder.
        if image_source != "stock" and pexels_key is not None:
            stock_query = _to_stock_query(prompt)
            logger.info(
                "image provider: AI chain failed for prompt %d, "
                "trying Pexels fallback with query=%r",
                idx + 1, stock_query,
            )
            result = await _try_stock(
                prompt, out_path, idx, query_override=stock_query,
            )
            if result is not None:
                return result
        # Final fallback — gradient placeholder.
        return poll.write_placeholder(out_path, width=width, height=height)

    return await asyncio.gather(*[_one(i, p) for i, p in enumerate(prompts)])


__all__ = ["generate_batch"]
