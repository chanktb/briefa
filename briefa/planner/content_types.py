"""Content classification + voice rate/name maps keyed by ContentType.

Used by both the planner (to pick rate/voice) and the TTS router (to apply rate
to a provider call). Kept in `planner/` because ContentType originates from the
planner's input classification.
"""
from __future__ import annotations

from enum import StrEnum

# ────────────────────── ENUMS ──────────────────────

class ContentType(StrEnum):
    NEWS = "news"
    LEARNING = "learning"
    STORY = "story"
    TECH = "tech"     # tech/dev/AI/code — uses male voice


# ────────────────────── VOICE RATE MAP ──────────────────────

# Edge-TTS rate flag values keyed by content type.
# Tuned against Vietnamese voices (slower than EN baseline).
#
# These values mirror the upstream ktb-studio settings — the briefa
# tool in that monorepo runs at +35% on vi-VN-NamMinhNeural (male
# broadcaster) and reads at VTV pace. The earlier attempt to bump NEWS to
# +50% / +70% was solving the wrong problem: the slowness came from the
# default female voice (vi-VN-HoaiMyNeural drags at any rate), not from
# the rate value. Channels here ship with the male broadcaster voice
# matching upstream, so the upstream rate works as-is.
VOICE_RATE_MAP: dict[ContentType, str] = {
    ContentType.NEWS:     "+35%",
    ContentType.LEARNING: "+0%",
    ContentType.STORY:    "+10%",
    ContentType.TECH:     "+15%",
}
VOICE_RATE_BY_TYPE = VOICE_RATE_MAP


# ────────────────────── EDGE TTS VOICES ──────────────────────

# Default Vietnamese voices on Microsoft Edge TTS.
DEFAULT_VOICE_NAME = "vi-VN-HoaiMyNeural"          # female
DEFAULT_VOICE_NAME_MALE = "vi-VN-NamMinhNeural"    # male

# Voice by content type. TECH/LEARNING default to male; NEWS/STORY are
# placeholders — the planner randomizes per-video for those types.
VOICE_NAME_BY_TYPE: dict[ContentType, str] = {
    ContentType.NEWS:     DEFAULT_VOICE_NAME,
    ContentType.LEARNING: DEFAULT_VOICE_NAME_MALE,
    ContentType.STORY:    DEFAULT_VOICE_NAME,
    ContentType.TECH:     DEFAULT_VOICE_NAME_MALE,
}


# ────────────────────── GOOGLE CHIRP 3 HD VOICES ──────────────────────

# Same mapping logic as Edge, but for Google Cloud TTS Chirp 3 HD voices.
GOOGLE_TTS_DEFAULT_MALE   = "vi-VN-Chirp3-HD-Charon"       # broadcaster, neutral pro
GOOGLE_TTS_DEFAULT_FEMALE = "vi-VN-Chirp3-HD-Achernar"     # warm natural

GOOGLE_TTS_VOICE_BY_TYPE: dict[ContentType, str] = {
    ContentType.NEWS:     GOOGLE_TTS_DEFAULT_FEMALE,
    ContentType.LEARNING: GOOGLE_TTS_DEFAULT_MALE,
    ContentType.STORY:    GOOGLE_TTS_DEFAULT_FEMALE,
    ContentType.TECH:     GOOGLE_TTS_DEFAULT_MALE,
}

# Random-pick pools for NEWS/STORY — each video picks one voice and uses it
# across all scenes for consistency.
GOOGLE_TTS_POOL_MALE: list[str] = [
    "vi-VN-Chirp3-HD-Charon",
    "vi-VN-Chirp3-HD-Algenib",
    "vi-VN-Chirp3-HD-Iapetus",
    "vi-VN-Chirp3-HD-Puck",
    "vi-VN-Chirp3-HD-Schedar",
]
GOOGLE_TTS_POOL_FEMALE: list[str] = [
    "vi-VN-Chirp3-HD-Achernar",
    "vi-VN-Chirp3-HD-Kore",
    "vi-VN-Chirp3-HD-Zephyr",
    "vi-VN-Chirp3-HD-Aoede",
    "vi-VN-Chirp3-HD-Leda",
]


__all__ = [
    "ContentType",
    "VOICE_RATE_MAP",
    "VOICE_RATE_BY_TYPE",
    "DEFAULT_VOICE_NAME",
    "DEFAULT_VOICE_NAME_MALE",
    "VOICE_NAME_BY_TYPE",
    "GOOGLE_TTS_DEFAULT_MALE",
    "GOOGLE_TTS_DEFAULT_FEMALE",
    "GOOGLE_TTS_VOICE_BY_TYPE",
    "GOOGLE_TTS_POOL_MALE",
    "GOOGLE_TTS_POOL_FEMALE",
]
