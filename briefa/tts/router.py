"""Provider-chain TTS router.

Tries each enabled provider in order, writing the MP3 to the destination
path on the first success. The result is cached by ``(text, voice, rate,
provider)`` so repeated runs of the renderer reuse existing audio.

Default chain:

    Google Chirp 3 HD  →  ElevenLabs  →  Edge TTS  →  gTTS

Each provider is opt-in:
  - Google needs ``google_tts_api_key`` + ``google_tts_voice`` from the
    channel config.
  - ElevenLabs needs ``ELEVENLABS_API_KEY`` in the environment plus
    ``elevenlabs_voice_id`` from the channel config.
  - Edge always runs (no key, no quota) — but anh hit a network-level
    WSServerHandshakeError 403 on his ISP that kills Edge entirely.
  - gTTS final fallback when Edge fails: Google Translate's public TTS
    endpoint, different infrastructure, no key. Lower quality + only a
    binary slow/normal rate, but it works through firewalls that block
    speech.platform.bing.com.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from . import elevenlabs as eleven
from . import google as google_tts
from .edge import normalize_rate, synth_edge
from .elevenlabs import synth_elevenlabs
from .google import synth_google
from .gtts_adapter import synth_gtts

logger = logging.getLogger("briefa.tts.router")


def _cache_key(text: str, voice: str, rate: str, provider: str) -> str:
    """12-hex SHA-1 prefix keyed by all knobs that affect the output."""
    h = hashlib.sha1()
    for part in (text, voice, rate, provider):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:12]


def _try_cache(cache_path: Path, out_path: Path) -> bool:
    """Copy a cached MP3 to ``out_path`` if it exists and is non-empty."""
    if cache_path.exists() and cache_path.stat().st_size > 0:
        out_path.write_bytes(cache_path.read_bytes())
        return True
    return False


def _save_cache(out_path: Path, cache_path: Path) -> None:
    try:
        cache_path.write_bytes(out_path.read_bytes())
    except OSError as exc:
        logger.debug("cache write skipped (%s): %s", cache_path, exc)


async def generate_voice(
    text: str,
    voice_name: str,
    rate: str,
    out_path: Path,
    *,
    elevenlabs_voice_id: str = "",
    google_tts_api_key: str = "",
    google_tts_voice: str = "",
    cache_dir: Path | None = None,
) -> Path:
    """Synthesize ``text`` to ``out_path`` via the provider chain.

    Args:
        text:                 Voice script (no SSML).
        voice_name:           Edge TTS voice short name (used by the final fallback).
        rate:                 Edge-style rate ``"+25%"`` — providers convert internally.
        out_path:             Destination MP3.
        elevenlabs_voice_id:  Empty disables ElevenLabs even if the env key is set.
        google_tts_api_key:   Empty disables Google TTS.
        google_tts_voice:     Google voice name, e.g. ``"vi-VN-Chirp3-HD-Achernar"``.
        cache_dir:            Optional cache directory. Defaults to
                              ``<out_path.parent>/.tts_cache``.

    Returns:
        ``out_path`` on success. Always returns — Edge fallback is treated
        as guaranteed; any exception in Edge propagates so the caller can
        surface the failure.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(cache_dir) if cache_dir is not None else out_path.parent / ".tts_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    rate_norm = normalize_rate(rate)

    # ── Provider 1: Google Chirp 3 HD ──
    if google_tts_api_key and google_tts_voice and not google_tts.is_dead():
        cache_g = cache_dir / f"{_cache_key(text, google_tts_voice, rate_norm, 'google')}.mp3"
        if _try_cache(cache_g, out_path):
            return out_path
        if await synth_google(
            text=text,
            voice_name=google_tts_voice,
            rate=rate_norm,
            api_key=google_tts_api_key,
            out_path=out_path,
        ):
            _save_cache(out_path, cache_g)
            return out_path
        # else: fall through

    # ── Provider 2: ElevenLabs ──
    if elevenlabs_voice_id and not eleven.is_dead():
        cache_el = cache_dir / f"{_cache_key(text, elevenlabs_voice_id, 'n/a', 'elevenlabs')}.mp3"
        if _try_cache(cache_el, out_path):
            return out_path
        if await synth_elevenlabs(text=text, voice_id=elevenlabs_voice_id, out_path=out_path):
            _save_cache(out_path, cache_el)
            return out_path

    # ── Provider 3: Edge TTS ──
    cache_edge = cache_dir / f"{_cache_key(text, voice_name, rate_norm, 'edge')}.mp3"
    if _try_cache(cache_edge, out_path):
        return out_path
    try:
        await synth_edge(text=text, voice_name=voice_name, rate=rate_norm, out_path=out_path)
    except Exception as exc:
        # Default: FAIL HARD. For briefa a half-female / half-male
        # video is worse than no video — the audience hears a brand
        # voice switch mid-clip and it reads as broken production. The
        # caller (news.py) prints a clean error and the operator re-runs
        # later when Microsoft's per-voice limit refills.
        #
        # Opt-in to gTTS fallback by setting BRIEFA_ALLOW_GTTS=1
        # in .env — useful only in emergencies (deadline hit, must
        # ship something) where mixed-voice is preferable to nothing.
        import os
        import sys
        allow_gtts = (os.environ.get("BRIEFA_ALLOW_GTTS") or "").strip() == "1"
        if not allow_gtts:
            sys.stderr.write(
                "\n"
                "================================================================\n"
                f"!! EDGE TTS FAILED after retries + locale fallback: {exc!s}\n"
                f"!! Requested voice={voice_name!r} rate={rate_norm!r}.\n"
                "!! Pipeline ABORTED to preserve brand voice consistency.\n"
                "!! \n"
                "!! Microsoft Edge TTS is rate-limiting / browning-out the\n"
                "!! Vietnamese voices right now. Wait 10-30 minutes and\n"
                "!! re-run; the limit refills.\n"
                "!! \n"
                "!! If you MUST ship a video now and a mixed-voice video\n"
                "!! is acceptable, set BRIEFA_ALLOW_GTTS=1 in .env\n"
                "!! to fall through to gTTS (female VN voice, no rate\n"
                "!! control — sounds nothing like NamMinh).\n"
                "================================================================\n"
            )
            raise RuntimeError(
                f"Edge TTS failed and gTTS fallback is disabled. "
                f"Set BRIEFA_ALLOW_GTTS=1 to override. Last error: {exc}"
            ) from exc
        sys.stderr.write(
            "\n"
            "================================================================\n"
            f"!! EDGE TTS FAILED, BRIEFA_ALLOW_GTTS=1 — falling to gTTS\n"
            f"!! Voice {voice_name!r} rate {rate_norm!r} will NOT match output.\n"
            "================================================================\n"
        )
        logger.warning("edge-tts failed (%s) — falling back to gTTS (opt-in)", exc)
    else:
        _save_cache(out_path, cache_edge)
        return out_path

    # ── Provider 4: gTTS (opt-in last-ditch fallback) ──
    cache_gtts = cache_dir / f"{_cache_key(text, voice_name, rate_norm, 'gtts')}.mp3"
    if _try_cache(cache_gtts, out_path):
        return out_path
    if await synth_gtts(text=text, voice_name=voice_name, rate=rate_norm, out_path=out_path):
        _save_cache(out_path, cache_gtts)
        return out_path
    raise RuntimeError(
        "all TTS providers failed (Google / ElevenLabs / Edge / gTTS) — "
        "check network connectivity + provider keys"
    )


__all__ = ["generate_voice"]
