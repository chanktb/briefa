"""Cloudflare Workers AI image gen client — fast & free (10k Neurons/day).

Endpoint::

    POST https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/run/{MODEL}

Auth: ``Authorization: Bearer {API_TOKEN}``.

Models (vertical 9:16 friendly):
  - ``@cf/black-forest-labs/flux-1-schnell`` — FAST (4 steps, ~2-4s),
    great quality. PRIMARY default.
  - ``@cf/lykon/dreamshaper-8-lcm``         — fast SDXL, anime/cartoon
    friendly.
  - ``@cf/stabilityai/stable-diffusion-xl-base-1.0`` — slower SDXL,
    photoreal.

Response shape varies per model: JSON (``{"result":{"image": base64}}``)
or raw PNG bytes. We try JSON first and fall back to bytes.

Free tier: 10 000 Neurons/day. ``flux-schnell`` at default settings costs
~5 Neurons / image → hundreds of videos/day without paying.

Get credentials at https://dash.cloudflare.com/ → Workers AI → "Use REST
API". Two env vars wire it into the rest of ``ktb-studio``::

    CF_ACCOUNT_ID=...
    CF_AI_TOKEN=...

Channels can override per-brand via the same names (read by
:mod:`core.images.image_provider`).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
from pathlib import Path

import httpx

logger = logging.getLogger("briefa.images.cloudflare")

DEFAULT_MODEL = "@cf/black-forest-labs/flux-1-schnell"
DEFAULT_STEPS = 4

# SDXL models train at 1024×1024 — forcing 1080×1920 here would stretch
# compositions. We generate at the model's sweet spot and let the renderer
# crop to 9:16 via ``background-size: cover``.
DEFAULT_WIDTH = 1024
DEFAULT_HEIGHT = 1024
HTTP_TIMEOUT = 60.0
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0


def resolve_credentials() -> tuple[str, str] | None:
    """Return ONE ``(account_id, api_token)`` from env, or ``None`` when unset.

    Compatibility wrapper for callers that only need a single set of
    creds (e.g. to decide "should I attempt CF at all"). New code should
    call :func:`resolve_all_credentials` and let
    :func:`generate_image` iterate through them on 429.
    """
    pairs = resolve_all_credentials()
    if not pairs:
        return None
    import random
    return random.choice(pairs)


def resolve_all_credentials() -> list[tuple[str, str]]:
    """Return EVERY known ``(account_id, api_token)`` pair, shuffled.

    Multi-account rotation: ``CF_ACCOUNTS`` (plural, semicolon-separated)
    lets a deployment spread the 10 k Neurons/day free-tier cap across
    multiple personal CF accounts. Each entry is ``account_id:api_token``,
    e.g. ``a1:tok1;a2:tok2``. Single-account ``CF_ACCOUNT_ID`` +
    ``CF_AI_TOKEN`` still works as a fallback.

    :func:`generate_image` iterates through this list on 429 — once one
    account's daily 10 k Neuron quota burns out, the next account picks
    up the slack instead of falling through to Pollinations / Pexels.
    """
    accounts_csv = (os.environ.get("CF_ACCOUNTS") or "").strip()
    pairs: list[tuple[str, str]] = []
    if accounts_csv:
        for piece in accounts_csv.split(";"):
            piece = piece.strip()
            if ":" not in piece:
                continue
            aid, tok = piece.split(":", 1)
            aid, tok = aid.strip(), tok.strip()
            if aid and tok:
                pairs.append((aid, tok))

    if not pairs:
        account_id = (os.environ.get("CF_ACCOUNT_ID") or "").strip()
        api_token = (os.environ.get("CF_AI_TOKEN") or "").strip()
        if account_id and api_token:
            pairs.append((account_id, api_token))

    if pairs:
        import random
        random.shuffle(pairs)
    return pairs


async def generate_image(
    prompt: str,
    out_path: Path,
    *,
    account_id: str | None = None,
    api_token: str | None = None,
    model: str = DEFAULT_MODEL,
    steps: int = DEFAULT_STEPS,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    seed: int | None = None,
    style_suffix: str = "",
) -> Path:
    """Generate one image via Cloudflare Workers AI; save it to ``out_path``.

    Returns ``out_path`` on success.

    Raises:
        RuntimeError: when all retries fail. Callers should catch and fall
            through to a different provider or the placeholder gradient.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    full_prompt = (prompt or "").strip()
    if style_suffix:
        full_prompt = f"{full_prompt}{style_suffix}"

    # Decide which CF accounts to try. Explicit creds → just that one.
    # Otherwise: pull all accounts from env so 429 on one rolls forward
    # to the next instead of falling through to Pollinations / Pexels.
    if account_id and api_token:
        creds_list = [(account_id, api_token)]
    else:
        creds_list = resolve_all_credentials()
    if not creds_list:
        raise RuntimeError(
            "CF: no credentials available (set CF_ACCOUNTS or "
            "CF_ACCOUNT_ID + CF_AI_TOKEN)"
        )

    payload: dict = {
        "prompt": full_prompt,
        "steps": steps,
        "width": width,
        "height": height,
    }
    if seed is not None:
        payload["seed"] = int(seed)

    last_err: Exception | None = None
    # Outer loop = accounts. Inner loop = retries per account. Once an
    # account returns 429 we DON'T waste retries on it — we move to the
    # next account immediately (a 429 means quota exhausted, no amount
    # of retrying helps until midnight UTC).
    for cred_idx, (aid, tok) in enumerate(creds_list, 1):
        url = f"https://api.cloudflare.com/client/v4/accounts/{aid}/ai/run/{model}"
        headers = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
        quota_exhausted = False
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                    resp = await client.post(url, headers=headers, json=payload)

                if resp.status_code == 200:
                    content_type = resp.headers.get("content-type", "")
                    if "json" in content_type:
                        data = resp.json()
                        if not data.get("success", True):
                            errors = data.get("errors", [])
                            raise RuntimeError(f"CF AI errors: {errors}")
                        result = data.get("result", {})
                        b64 = result.get("image") or result.get("image_b64")
                        if b64:
                            img_bytes = base64.b64decode(b64)
                            out_path.write_bytes(img_bytes)
                            logger.info(
                                "cf-ai saved %s (%d KB, account %d/%d, attempt %d, model=%s)",
                                out_path.name, len(img_bytes) // 1024,
                                cred_idx, len(creds_list), attempt, model,
                            )
                            return out_path
                        raise RuntimeError(
                            f"CF AI 200 but no image in result: keys={list(result.keys())}"
                        )
                    # Raw binary
                    if resp.content:
                        out_path.write_bytes(resp.content)
                        if _is_blank_or_black(out_path):
                            with contextlib.suppress(OSError):
                                out_path.unlink()
                            raise RuntimeError(
                                "CF AI returned blank/black image (likely NSFW filter)"
                            )
                        logger.info(
                            "cf-ai saved %s (raw, %d KB, account %d/%d, attempt %d)",
                            out_path.name, len(resp.content) // 1024,
                            cred_idx, len(creds_list), attempt,
                        )
                        return out_path
                    raise RuntimeError("CF AI 200 but empty body")

                last_err = RuntimeError(
                    f"CF AI HTTP {resp.status_code} (account {cred_idx}/{len(creds_list)}): "
                    f"{resp.text[:300]}"
                )
                logger.warning(
                    "cf-ai account %d/%d attempt %d/%d -> %s",
                    cred_idx, len(creds_list), attempt, MAX_RETRIES, last_err,
                )
                # 429 = daily quota exhausted on THIS account. Don't waste
                # retries — bail to the next account immediately.
                if resp.status_code == 429:
                    quota_exhausted = True
                    break
                # 401/403 = bad credentials — also no point retrying THIS
                # account, but try the next one in case one of the env
                # entries is mistyped.
                if resp.status_code in (401, 403):
                    break
            except (httpx.HTTPError, httpx.ReadTimeout) as exc:
                last_err = exc
                logger.warning(
                    "cf-ai account %d/%d attempt %d/%d network err: %s",
                    cred_idx, len(creds_list), attempt, MAX_RETRIES, exc,
                )
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)
        # End of this account's retries. Quota-exhausted accounts fall
        # through to the next account in creds_list; rate / other errors
        # also fall through so a single transient hiccup doesn't kill
        # the whole call when multiple accounts are available.
        if quota_exhausted:
            logger.info(
                "cf-ai account %d quota exhausted — trying account %d",
                cred_idx, cred_idx + 1,
            )

    raise RuntimeError(
        f"CF AI failed across all {len(creds_list)} account(s): {last_err}"
    )


def _is_blank_or_black(path: Path, *, dark_threshold: int = 16, ratio: float = 0.95) -> bool:
    """``True`` when the saved image is mostly black — likely an NSFW filter trip.

    SDXL Lightning returns a fully-black PNG with HTTP 200 when its safety
    filter fires, so we sample a small thumbnail and look at the dark-pixel
    fraction. Treats unreadable / tiny files as blank too.
    """
    try:
        from PIL import Image
        with Image.open(path) as img:
            img = img.convert("L")
            img.thumbnail((64, 64))
            pixels = list(img.getdata())
            if not pixels:
                return True
            dark = sum(1 for p in pixels if p < dark_threshold)
            mean = sum(pixels) / len(pixels)
            return (dark / len(pixels) >= ratio) or (mean < dark_threshold)
    except (OSError, ValueError):
        return path.stat().st_size < 2048


__all__ = [
    "DEFAULT_HEIGHT",
    "DEFAULT_MODEL",
    "DEFAULT_STEPS",
    "DEFAULT_WIDTH",
    "generate_image",
    "resolve_credentials",
]
