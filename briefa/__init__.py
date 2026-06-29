"""KTB AI News — text / URL / markdown / image → vertical 9:16 short video.

Free stack: Gemini Flash Lite (planner + vision) + Edge TTS + Cloudflare
FLUX + HyperFrames renderer. Vendored from ktb-studio/core, hardened with
the lessons learned in ktb-news-editor:

  - edge-tts >= 7.0 (6.1.x is dead since 2026)
  - per-voice 250ms throttle + exponential retry on NoAudioReceived
  - gTTS fallback is opt-in via BRIEFA_ALLOW_GTTS=1
"""

__version__ = "0.1.0"
