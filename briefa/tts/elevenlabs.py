"""ElevenLabs Text-to-Speech provider — premium quality, optional.

ElevenLabs is only used when both ``ELEVENLABS_API_KEY`` is in the
environment and the channel config supplies an ``elevenlabs_voice_id``.

Configurable via env vars:
  - ELEVENLABS_API_KEY         (required for the provider to fire)
  - ELEVENLABS_MODEL           (default: ``eleven_multilingual_v2``)
  - ELEVENLABS_STABILITY       (default: ``0.5``)
  - ELEVENLABS_SIMILARITY      (default: ``0.75``)

Same kill-switch pattern as :mod:`core.tts.google`: 401 / 402 / 429 → mark
the session dead and skip on subsequent calls.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx

logger = logging.getLogger("briefa.tts.elevenlabs")

_API_BASE = "https://api.elevenlabs.io/v1/text-to-speech"
_REQUEST_TIMEOUT = 60.0

_DEAD = False


def is_dead() -> bool:
    return _DEAD


def reset_dead_flag() -> None:
    global _DEAD
    _DEAD = False


async def synth_elevenlabs(text: str, voice_id: str, out_path: Path) -> bool:
    """Synthesize ``text`` via ElevenLabs and write MP3 to ``out_path``.

    Returns ``True`` on success, ``False`` on any failure so the router can
    fall through. Sets the session kill switch on 401 / 402 / 429.
    """
    global _DEAD
    if _DEAD:
        return False
    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not api_key or not voice_id:
        return False

    model_id = os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2")
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": float(os.environ.get("ELEVENLABS_STABILITY", "0.5")),
            "similarity_boost": float(os.environ.get("ELEVENLABS_SIMILARITY", "0.75")),
        },
    }
    headers = {"xi-api-key": api_key, "Content-Type": "application/json"}
    url = f"{_API_BASE}/{voice_id}"

    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            r = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("ElevenLabs network error, falling through: %s", exc)
        return False

    if r.status_code in (401, 402, 429):
        _DEAD = True
        logger.warning(
            "ElevenLabs disabled for rest of session (status=%d): %s",
            r.status_code, r.text[:200],
        )
        return False

    if r.status_code != 200 or not r.content:
        logger.warning("ElevenLabs HTTP %d, falling through: %s", r.status_code, r.text[:200])
        return False

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(r.content)
    logger.info(
        "ElevenLabs OK voice=%s bytes=%d chars=%d", voice_id, len(r.content), len(text)
    )
    return True


__all__ = ["synth_elevenlabs", "is_dead", "reset_dead_flag"]
