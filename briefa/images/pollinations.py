"""Pollinations.ai image generation client — FREE, no API key.

Public endpoint::

    https://image.pollinations.ai/prompt/{url-encoded prompt}?width=...&height=...

The free tier silently downgrades anything above 720×1280 (and sometimes
returns HTTP 402 under load). Stay inside that envelope — the composer
upscales / crops to the final HyperFrames canvas afterwards.

Latency is unpredictable: cached prompts return in ~1-3 s, cold queue can
take 60-90 s. The retry loop uses generous 5/10/15 s back-offs because the
free tier rate-limits rapid retries. After all retries fail, the client
writes a plain coloured-gradient PNG so the renderer never blocks.

Concurrency: the public free endpoint hates parallel bursts. Use
:func:`generate_batch` with the default ``max_concurrent=1`` unless you
benchmarked otherwise.
"""
from __future__ import annotations

import asyncio
import logging
import urllib.parse
from pathlib import Path

import httpx
from PIL import Image, ImageDraw

logger = logging.getLogger("briefa.images.pollinations")

POLLINATIONS_BASE = "https://image.pollinations.ai/prompt/"
DEFAULT_MODEL = "flux"
DEFAULT_WIDTH = 720
DEFAULT_HEIGHT = 1280
HTTP_TIMEOUT = 120.0
MAX_RETRIES = 4
RETRY_BACKOFF_SECONDS = 5.0  # 5 s, 10 s, 15 s — free tier hates rapid retries

# Style guidance appended to every prompt so output stays on-brand and SFW.
DEFAULT_STYLE_SUFFIX = (
    " — cartoon illustration, vibrant colors, clean composition, "
    "vertical 9:16, no text, professional design, "
    "modest, fully clothed, family friendly, safe for work"
)


# ════════════════════════════════════════════════════════════════════════
# Single-image generation
# ════════════════════════════════════════════════════════════════════════

async def generate_image(
    prompt: str,
    out_path: Path,
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    model: str = DEFAULT_MODEL,
    seed: int | None = None,
    style_suffix: str = DEFAULT_STYLE_SUFFIX,
    private: bool = True,
) -> Path:
    """Generate one image and save it to ``out_path``.

    Args:
        prompt:       Free-text scene description.
        out_path:     Destination JPG / PNG path.
        width:        Up to 720 on the free tier. Composer upscales later.
        height:       Up to 1280 on the free tier.
        model:        ``"flux"`` (default), ``"flux-realism"``, ``"turbo"``.
        seed:         Pass an int for deterministic repeats (e.g. ``scene_index * 100``).
        style_suffix: Appended to ``prompt`` before encoding. Default keeps
                      output SFW + cartoon. Pass ``""`` to disable.
        private:      ``True`` skips the public Pollinations gallery.

    Returns:
        ``out_path`` on success.

    Raises:
        RuntimeError: if every retry failed. Callers should catch and fall
            back to :func:`write_placeholder`.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    full_prompt = (prompt or "").strip()
    if style_suffix:
        full_prompt = f"{full_prompt}{style_suffix}"
    encoded = urllib.parse.quote(full_prompt, safe="")
    url = f"{POLLINATIONS_BASE}{encoded}"
    params: dict[str, str | int] = {
        "width": width,
        "height": height,
        "model": model,
        "nologo": "true",
    }
    if seed is not None:
        params["seed"] = int(seed)
    if private:
        params["private"] = "true"

    last_err: Exception | None = None
    last_status: int | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                resp = await client.get(url, params=params)
            last_status = resp.status_code
            if resp.status_code == 200 and resp.content:
                out_path.write_bytes(resp.content)
                logger.info(
                    "pollinations saved %s (%d KB, attempt %d, model=%s)",
                    out_path.name, len(resp.content) // 1024, attempt, model,
                )
                return out_path
            last_err = RuntimeError(
                f"pollinations HTTP {resp.status_code} ({len(resp.content)} bytes)"
            )
            # 402 Payment Required means the gateway has gated the model
            # behind a paywall. Retrying the SAME model won't help — break
            # out so the caller can try a different model or fall through
            # to the placeholder.
            if resp.status_code == 402:
                logger.warning(
                    "pollinations model=%s returned 402 Payment Required — "
                    "abandoning this model (retries skipped)",
                    model,
                )
                break
        except (httpx.HTTPError, httpx.ReadTimeout) as exc:
            last_err = exc
        if attempt < MAX_RETRIES:
            wait = RETRY_BACKOFF_SECONDS * attempt
            logger.warning(
                "pollinations attempt %d/%d failed (%s) — sleeping %.1fs",
                attempt, MAX_RETRIES, last_err, wait,
            )
            await asyncio.sleep(wait)

    suffix = " (HTTP 402 paywall)" if last_status == 402 else ""
    raise RuntimeError(
        f"pollinations gave up after {attempt} attempt(s){suffix}: {last_err}"
    )


# ════════════════════════════════════════════════════════════════════════
# Placeholder gradient (silent fallback)
# ════════════════════════════════════════════════════════════════════════

def write_placeholder(
    out_path: Path,
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    top_color: tuple[int, int, int] = (34, 211, 238),     # cyan
    bottom_color: tuple[int, int, int] = (74, 222, 128),  # green
) -> Path:
    """Write a vertical-gradient JPG so the renderer never blocks on missing assets.

    Used as the final fallback when both the primary image provider and any
    retry path fail.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (width, height), top_color)
    draw = ImageDraw.Draw(img)
    h = max(1, height - 1)
    for y in range(height):
        t = y / h
        r = int(top_color[0] + (bottom_color[0] - top_color[0]) * t)
        g = int(top_color[1] + (bottom_color[1] - top_color[1]) * t)
        b = int(top_color[2] + (bottom_color[2] - top_color[2]) * t)
        draw.line([(0, y), (width, y)], fill=(r, g, b))
    img.save(out_path, format="JPEG", quality=82)
    return out_path


# ════════════════════════════════════════════════════════════════════════
# Batch (parallel-safe)
# ════════════════════════════════════════════════════════════════════════

# Models to try in order when Pollinations 402s on the primary. ``flux`` is
# the high-quality default; ``turbo`` is the faster diffusion path that's
# still sometimes free; ``flux-realism`` is a third tier that handles
# different prompt shapes. Add / reorder via the ``model_chain`` kwarg.
DEFAULT_MODEL_CHAIN: list[str] = ["flux", "turbo", "flux-realism"]


async def generate_batch(
    prompts: list[str],
    out_dir: Path,
    *,
    style_suffix: str = DEFAULT_STYLE_SUFFIX,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    model: str = DEFAULT_MODEL,
    model_chain: list[str] | None = None,
    max_concurrent: int = 1,
    filename_prefix: str = "scene",
) -> list[Path]:
    """Generate one image per prompt; never raises.

    When the primary ``model`` returns HTTP 402 (Pollinations paywall),
    each prompt is retried against the next model in ``model_chain``
    before falling through to :func:`write_placeholder`. The return list
    always has ``len(prompts)`` entries.

    The default ``max_concurrent=1`` matches Pollinations' free-tier
    tolerance; bump it only if you've benchmarked your endpoint quota.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(max(1, max_concurrent))
    # Stitch the explicit ``model`` arg to the head of the chain (deduped)
    # so a caller pinning ``model="turbo"`` doesn't get flux retried first.
    chain: list[str] = []
    for m in [model, *(model_chain or DEFAULT_MODEL_CHAIN)]:
        if m and m not in chain:
            chain.append(m)

    async def _one(idx: int, prompt: str) -> Path:
        async with sem:
            out_path = out_dir / f"{filename_prefix}_{idx + 1}.jpg"
            last_err: Exception | None = None
            for candidate in chain:
                try:
                    return await generate_image(
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
                    is_402 = "402" in str(exc)
                    if is_402 and candidate is not chain[-1]:
                        logger.info(
                            "pollinations prompt %d: %s paywalled — trying %s",
                            idx + 1, candidate,
                            chain[chain.index(candidate) + 1],
                        )
                        continue
                    # Network / connection error or last model in chain — bail.
                    break
            logger.warning(
                "pollinations failed for prompt %d across %d model(s) (%s) — "
                "writing placeholder",
                idx + 1, len(chain), last_err,
            )
            return write_placeholder(out_path, width=width, height=height)

    return await asyncio.gather(*[_one(i, p) for i, p in enumerate(prompts)])


__all__ = [
    "DEFAULT_HEIGHT",
    "DEFAULT_MODEL",
    "DEFAULT_STYLE_SUFFIX",
    "DEFAULT_WIDTH",
    "generate_batch",
    "generate_image",
    "write_placeholder",
]
