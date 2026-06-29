"""gTTS adapter — Google Translate's public TTS endpoint.

Different infrastructure from Microsoft Edge TTS, so it routes around
the WSServerHandshakeError 403 anh hit on his ISP. No API key, no
auth, free.

Trade-offs vs Edge:
  - One voice per language (vi vs en), no neural voice variety
  - Rate control limited to "slow=True/False" — we honour the slow flag
    when the requested rate is < 0%, otherwise normal speed
  - Slightly more synthesized-sounding than Edge but understandable

Used as the FINAL fallback in the TTS router chain; only kicks in when
Google Cloud TTS, ElevenLabs, AND Edge have all bailed.
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

logger = logging.getLogger("briefa.tts.gtts_adapter")


_VOICE_LANG_RE = re.compile(r"^([a-z]{2})[-_]([A-Z]{2})", re.IGNORECASE)


def _voice_to_gtts_lang(voice_name: str) -> str:
    """Extract a gTTS language code from an Edge-style voice short name.

    Examples:
        ``vi-VN-NamMinhNeural`` → ``"vi"``
        ``en-US-GuyNeural``     → ``"en"``
        ``vi``                  → ``"vi"`` (already a bare code)
    """
    if not voice_name:
        return "vi"
    m = _VOICE_LANG_RE.match(voice_name)
    if m:
        return m.group(1).lower()
    return voice_name.lower()[:2] or "vi"


def _rate_to_slow(rate: str) -> bool:
    """Map an Edge-style ``"+25%"`` rate to gTTS's binary slow flag."""
    if not rate:
        return False
    try:
        n = int(rate.rstrip("%"))
    except ValueError:
        return False
    return n < -20


def _synth_sync(text: str, voice_name: str, rate: str, out_path: Path) -> None:
    """Synchronous worker — runs in a thread pool so the caller stays async."""
    try:
        from gtts import gTTS
    except ImportError as exc:
        raise RuntimeError(
            "gtts not installed — pip install gTTS (or re-run SETUP.bat)"
        ) from exc
    lang = _voice_to_gtts_lang(voice_name)
    slow = _rate_to_slow(rate)
    tts = gTTS(text=text, lang=lang, slow=slow)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tts.save(str(out_path))


async def synth_gtts(text: str, voice_name: str, rate: str, out_path: Path) -> bool:
    """Synthesize ``text`` via gTTS to ``out_path``.

    Returns:
        ``True`` on success, ``False`` when the synthesis raised — the
        router treats False as "try the next provider" (there isn't one
        after gTTS; the caller will see no audio file).
    """
    try:
        await asyncio.to_thread(_synth_sync, text, voice_name, rate, out_path)
    except Exception as exc:
        logger.warning("gTTS failed: %s", exc)
        return False
    if not out_path.exists() or out_path.stat().st_size == 0:
        logger.warning("gTTS wrote an empty file %s", out_path)
        return False
    logger.info(
        "gTTS saved %s (%d KB, lang=%s)",
        out_path.name, out_path.stat().st_size // 1024,
        _voice_to_gtts_lang(voice_name),
    )
    return True


__all__ = ["synth_gtts"]
