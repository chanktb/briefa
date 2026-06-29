"""Measure audio (MP3) duration with mutagen → ffprobe fallback.

Edge-TTS occasionally emits MP3 headers that ``mutagen`` can't parse; the
``ffprobe`` subprocess call is a reliable fallback.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger("briefa.utils.audio_measure")


def _ffprobe_bin() -> str:
    """Resolve ffprobe binary. Honors $FFMPEG_BIN env var (Windows-friendly)."""
    base = os.environ.get("FFMPEG_BIN", "")
    if base:
        candidate = Path(base) / ("ffprobe.exe" if os.name == "nt" else "ffprobe")
        if candidate.exists():
            return str(candidate)
    return "ffprobe"


def _ffprobe_duration(path: Path) -> float:
    """Run ffprobe and return duration in seconds. Raises if ffprobe fails."""
    result = subprocess.run(
        [
            _ffprobe_bin(),
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)
    return float(data["format"]["duration"])


def measure_audio_duration(path: Path | str) -> float:
    """Return the duration of an MP3 file in seconds.

    Tries ``mutagen.mp3.MP3`` first (fast, in-process). Falls back to ffprobe
    if mutagen returns None / non-positive / raises.

    Args:
        path: Path to an .mp3 file.

    Returns:
        Duration in seconds.

    Raises:
        FileNotFoundError: if the audio file does not exist.
        RuntimeError:      if both mutagen and ffprobe fail.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Audio file not found: {p}")

    # Attempt 1: mutagen
    try:
        from mutagen.mp3 import MP3  # local import — keeps module importable without mutagen

        m = MP3(str(p))
        if m.info is not None:
            length = getattr(m.info, "length", None)
            if length is not None and length > 0.05:
                return float(length)
    except Exception as exc:
        logger.debug("mutagen failed for %s: %s — falling back to ffprobe", p, exc)

    # Attempt 2: ffprobe fallback
    try:
        return _ffprobe_duration(p)
    except (subprocess.CalledProcessError, FileNotFoundError, KeyError, ValueError) as e:
        raise RuntimeError(
            f"Both mutagen and ffprobe failed to read duration for {p}: {e}"
        ) from e


__all__ = ["measure_audio_duration"]
