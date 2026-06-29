"""Microsoft Edge TTS provider — free, no auth, always available.

Edge TTS exhibits 2 failure modes in practice on the free public endpoint:

  1. **Brief rate-limit on the whole channel** → ``NoAudioReceived``.
     Fix: backoff retry on the SAME voice (3s, 7s, 15s).
  2. **Brown-out on ONE specific voice for several minutes** (often the
     primary Vietnamese / English voice during busy hours).
     Fix: after exhausting same-voice retries, try other adult voices in
     the same locale (e.g. vi-VN-NamMinh ↔ vi-VN-HoaiMy).

Without the retry layer, a single transient empty-stream response makes
the router fall through to gTTS — which has ONE female Vietnamese voice
with no rate control, producing the "slow as a turtle, female voice" bug
even when the requested voice is male NamMinh at +35%.

This module mirrors the proven retry pattern from
``projects/ktb-lingo-feeder/lingofeeder/tts.py`` (the lesson recorded in
MEMORY.md on 2026-06-10: when fixing edge-tts in one repo, port the fix
to the sister repos — they share patterns but not code).
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import sys
from pathlib import Path

import edge_tts

logger = logging.getLogger("briefa.tts.edge")

_RATE_RE = re.compile(r"^[+-]\d{1,3}%$")
# Longer backoffs than the upstream lingo-feeder values. CEO's render
# logs (2026-06-11) showed scenes still needing 3-4 retries with the
# 3 / 7 / 15 schedule — Microsoft is throttling the vi-VN voices more
# aggressively than when lingo-feeder was tuned. 5 / 15 / 30 / 60 gives
# the rate-limit budget more time to refill so we waste less wall-clock
# on dead-end attempts.
_BACKOFFS_SECONDS = (5.0, 15.0, 30.0, 60.0)
_MAX_SAME_VOICE_ATTEMPTS = 5  # was 4 — one extra attempt with the 60s wait
_MAX_FALLBACK_ATTEMPTS_PER_VOICE = 2

# Cache the Edge TTS voice list — fetched once per process.
_VOICES_CACHE: list[dict] | None = None
_VOICES_LOCK = asyncio.Lock()


def normalize_rate(rate: str) -> str:
    """Edge TTS requires ``+N%`` or ``-N%``.

    Coerces bare ``0%`` / ``25%`` to ``+0%`` / ``+25%`` so callers can pass
    either form. Falls back to ``+0%`` for empty / unparseable input.
    """
    r = (rate or "").strip()
    if _RATE_RE.match(r):
        return r
    if r and r[0].isdigit():
        return f"+{r}"
    return "+0%"


async def _load_voices() -> list[dict]:
    """Fetch and cache the Edge TTS voice catalog (process-lifetime)."""
    global _VOICES_CACHE
    if _VOICES_CACHE is None:
        async with _VOICES_LOCK:
            if _VOICES_CACHE is None:
                _VOICES_CACHE = await edge_tts.list_voices()
    return _VOICES_CACHE


def _is_adult_voice(v: dict) -> bool:
    """True when the voice is a general-purpose adult voice (excludes
    child / dialect / multilingual variants that often sound off for
    news delivery).
    """
    name = v.get("ShortName", "")
    if "Multilingual" in name:
        return False
    # Edge TTS marks child voices in the short name (e.g. en-US-AnaNeural is a
    # child voice). For Vietnamese there are only adult voices so this is
    # belt-and-suspenders for future locales.
    return True


async def _alternate_voices_for(voice: str) -> list[str]:
    """Other adult Edge TTS voices in the same locale, excluding ``voice``.

    Used as fallback when the requested voice keeps returning empty audio —
    Microsoft sometimes brown-outs a specific voice for minutes at a time
    but other voices in the locale keep working.
    """
    locale = "-".join(voice.split("-")[:2])  # "vi-VN-HoaiMyNeural" → "vi-VN"
    voices = await _load_voices()
    same_locale = [
        v for v in voices
        if v.get("Locale", "").lower() == locale.lower()
        and v.get("ShortName") != voice
        and _is_adult_voice(v)
    ]
    return [v["ShortName"] for v in same_locale]


async def synth_edge(text: str, voice_name: str, rate: str, out_path: Path) -> None:
    """Synthesize ``text`` to an MP3 at ``out_path`` via Edge TTS, with
    retry on ``NoAudioReceived`` and voice fallback when the primary
    voice browns out.

    Raises the last :class:`edge_tts.exceptions.NoAudioReceived` only
    when EVERY voice in the locale has been exhausted — in that case
    the caller (router) is expected to either bail or fall through to
    a different provider.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rate_norm = normalize_rate(rate)
    last_exc: Exception | None = None

    # ── Phase 1: retry the requested voice (covers transient rate-limits) ──
    for attempt in range(_MAX_SAME_VOICE_ATTEMPTS):
        try:
            comm = edge_tts.Communicate(text=text, voice=voice_name, rate=rate_norm)
            await comm.save(str(out_path))
            if attempt > 0:
                sys.stderr.write(
                    f"[edge-tts] recovered after {attempt + 1} attempt(s) "
                    f"on {voice_name!r}\n"
                )
            logger.info(
                "Edge TTS OK voice=%s rate=%s out=%s (attempt %d)",
                voice_name, rate_norm, out_path, attempt + 1,
            )
            return
        except edge_tts.exceptions.NoAudioReceived as exc:
            last_exc = exc
            if attempt < _MAX_SAME_VOICE_ATTEMPTS - 1:
                wait = _BACKOFFS_SECONDS[attempt]
                sys.stderr.write(
                    f"[edge-tts] NoAudioReceived "
                    f"attempt {attempt + 1}/{_MAX_SAME_VOICE_ATTEMPTS} — "
                    f"retrying in {wait}s (voice={voice_name!r}, "
                    f"text_len={len(text)})\n"
                )
                await asyncio.sleep(wait)

    # ── Phase 2: requested voice is unresponsive — try locale alternates ──
    try:
        alternates = await _alternate_voices_for(voice_name)
    except Exception as exc:
        logger.warning("could not load voice catalog for fallback: %s", exc)
        alternates = []

    for alt in alternates:
        sys.stderr.write(
            f"[edge-tts] {voice_name!r} unresponsive — "
            f"trying fallback voice {alt!r}\n"
        )
        for attempt in range(_MAX_FALLBACK_ATTEMPTS_PER_VOICE):
            try:
                comm = edge_tts.Communicate(text=text, voice=alt, rate=rate_norm)
                await comm.save(str(out_path))
                # Loud banner when the channel's requested voice was
                # silently substituted — for news that's a brand thing
                # the user MUST see (NamMinh male -> HoaiMy female swap
                # changes the whole feel of the video).
                sys.stderr.write(
                    "\n"
                    "================================================================\n"
                    f"!! EDGE TTS VOICE SUBSTITUTED\n"
                    f"!! Requested: {voice_name!r}  (unresponsive after retries)\n"
                    f"!! Actually used: {alt!r}\n"
                    f"!! Rate {rate_norm!r} preserved.\n"
                    "!! Microsoft browns-out individual voices for minutes\n"
                    "!! at a time. Re-render later if the voice matters.\n"
                    "================================================================\n"
                )
                logger.info(
                    "Edge TTS OK via fallback voice=%s (requested=%s) rate=%s",
                    alt, voice_name, rate_norm,
                )
                return
            except edge_tts.exceptions.NoAudioReceived as exc:
                last_exc = exc
                if attempt < _MAX_FALLBACK_ATTEMPTS_PER_VOICE - 1:
                    await asyncio.sleep(5.0)

    sys.stderr.write(
        f"[edge-tts] All voices in locale failed for text_len={len(text)}. "
        "Likely a regional Edge TTS outage — wait a few minutes and retry.\n"
    )
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(
        "Edge TTS exhausted all retries + locale fallbacks with no audio."
    )


__all__ = ["synth_edge", "normalize_rate"]
