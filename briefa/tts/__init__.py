"""Text-to-speech providers + router with fallback chain.

The router (:func:`core.tts.router.generate_voice`) is the high-level entry
point. Individual provider modules (:mod:`core.tts.google`,
:mod:`core.tts.elevenlabs`, :mod:`core.tts.edge`) can be called directly
when you want to exercise a single provider.

The phonetic post-processor (:func:`core.tts.phonetic.apply_en_phonetics`)
should be applied to the voice script before handing it to the router so
English tech terms are read with VN-friendly approximations.
"""

from .edge import normalize_rate, synth_edge
from .elevenlabs import synth_elevenlabs
from .google import rate_to_speaking_rate, synth_google
from .phonetic import EN_PHONETIC_MAP, apply_en_phonetics
from .router import generate_voice

__all__ = [
    "generate_voice",
    "apply_en_phonetics",
    "EN_PHONETIC_MAP",
    "synth_edge",
    "normalize_rate",
    "synth_google",
    "rate_to_speaking_rate",
    "synth_elevenlabs",
]
