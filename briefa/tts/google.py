"""Google Cloud Text-to-Speech provider — Chirp 3 HD via API key.

API key auth bypasses Service Account org policy blockers. Free tier covers
roughly 650 videos / month:
  - 1M chars / month Chirp 3 HD
  - 4M chars / month WaveNet
  - 1M chars / month Neural2

Endpoint:
  POST https://texttospeech.googleapis.com/v1/text:synthesize?key=KEY

Returns base64-encoded MP3 in ``audioContent``.

The module keeps a process-wide ``_DEAD`` kill switch: once the API returns
401 / 403 / 429 we stop hitting Google for the rest of the session. Reset
on process restart.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path

import httpx

logger = logging.getLogger("briefa.tts.google")

_API_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"
_RATE_RE = re.compile(r"^([+-]?)(\d{1,3})%?$")
_REQUEST_TIMEOUT = 60.0

# Process-wide kill switch — set to True after the first auth / quota failure
# so subsequent scenes skip straight to the next provider.
_DEAD = False


def is_dead() -> bool:
    """True when the API has signalled a fatal condition this session."""
    return _DEAD


def reset_dead_flag() -> None:
    """Clear the kill switch — useful in tests or after rotating credentials."""
    global _DEAD
    _DEAD = False


def rate_to_speaking_rate(edge_rate: str) -> float:
    """Convert an Edge-TTS rate string to Google's ``speakingRate`` float.

    ``"+25%"`` → ``1.25``. ``"-10%"`` → ``0.9``. Google's safe range is
    ``[0.25, 4.0]`` — values outside get clamped.
    """
    m = _RATE_RE.match((edge_rate or "+0%").strip())
    if not m:
        return 1.0
    sign, num = m.group(1), m.group(2)
    pct = int(num)
    if sign == "-":
        pct = -pct
    rate = 1.0 + pct / 100.0
    return max(0.25, min(4.0, rate))


async def synth_google(
    text: str,
    voice_name: str,
    rate: str,
    api_key: str,
    out_path: Path,
) -> bool:
    """Synthesize ``text`` via Google Chirp 3 HD and write MP3 to ``out_path``.

    Returns ``True`` on success, ``False`` on any failure so the router can
    fall through to the next provider.

    Args:
        text:       Voice script (≤5000 chars per Google's per-request limit).
        voice_name: Google voice name, e.g. ``"vi-VN-Chirp3-HD-Achernar"``.
        rate:       Edge-style rate string ``"+25%"`` — converted internally.
        api_key:    Google Cloud API key (restricted to TTS API + ideally IP).
        out_path:   Destination MP3 file.
    """
    global _DEAD
    if _DEAD:
        return False
    if not api_key or not voice_name or not text.strip():
        return False

    # Language code from voice prefix: "vi-VN-Chirp3-HD-X" → "vi-VN".
    lang_code = "-".join(voice_name.split("-")[:2]) if "-" in voice_name else "vi-VN"
    speaking_rate = rate_to_speaking_rate(rate)

    payload = {
        "input": {"text": text},
        "voice": {"languageCode": lang_code, "name": voice_name},
        "audioConfig": {
            "audioEncoding": "MP3",
            "speakingRate": speaking_rate,
            "sampleRateHertz": 24000,
        },
    }
    url = f"{_API_URL}?key={api_key}"

    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            r = await client.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
    except httpx.HTTPError as exc:
        logger.warning("Google TTS network error, falling through: %s", exc)
        return False

    if r.status_code in (401, 403):
        _DEAD = True
        logger.warning(
            "Google TTS auth failed (status=%d) — disabling for session: %s",
            r.status_code, r.text[:300],
        )
        return False
    if r.status_code == 429:
        _DEAD = True
        logger.warning("Google TTS quota exhausted (429) — disabling for session.")
        return False
    if r.status_code != 200:
        logger.warning("Google TTS HTTP %d, falling through: %s", r.status_code, r.text[:200])
        return False

    try:
        body = r.json()
    except json.JSONDecodeError:
        logger.warning("Google TTS returned malformed JSON")
        return False

    audio_b64 = body.get("audioContent", "")
    if not audio_b64:
        logger.warning("Google TTS returned empty audioContent")
        return False

    try:
        mp3_bytes = base64.b64decode(audio_b64)
    except (ValueError, TypeError) as exc:
        logger.warning("Google TTS base64 decode failed: %s", exc)
        return False

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(mp3_bytes)
    logger.info(
        "Google TTS OK voice=%s rate=%.2f bytes=%d chars=%d",
        voice_name, speaking_rate, len(mp3_bytes), len(text),
    )
    return True


__all__ = ["synth_google", "rate_to_speaking_rate", "is_dead", "reset_dead_flag"]
