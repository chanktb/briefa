"""Channel configuration — brand-scoped settings loaded from ``channels/<slug>/.env``.

Cross-cutting: consumed by TTS (voice), renderer (theme, avatar, watermark),
and poster (FB page tokens). Lives at the top of ``core/`` rather than under a
sub-package so each consumer imports it directly.

Real channels live in ``channels/<slug>/`` and are gitignored — only
``channels/example/`` ships in the public repo as a template.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .planner.content_types import DEFAULT_VOICE_NAME, DEFAULT_VOICE_NAME_MALE


class ChannelConfig(BaseModel):
    """Brand preset loaded from ``channels/<slug>/.env``.

    Defaults are neutral placeholders — every public-facing field gets
    overridden by the channel's own ``.env``. Missing keys fall back to these
    defaults; unknown keys are silently ignored so a channel can store extra
    metadata without breaking validation.
    """
    model_config = ConfigDict(extra="ignore")

    # ── Identity ──
    channel_name: str = "Your Brand"
    channel_handle: str = ""
    channel_watermark: str = ""
    # Optional small round avatar shown next to channel name (top-left).
    # File goes in channels/<slug>/static/<avatar_filename>. Empty = no avatar.
    avatar_filename: str = ""

    # ── Theme (CSS custom properties on the rendered scene) ──
    # ``theme_variant`` picks which template the renderer pulls in:
    #   "dark"   → core/renderer/themes/dark.css   (default, KhueForge feel)
    #   "bright" → core/renderer/themes/bright.css (newspaper editorial feel)
    # The numeric color knobs below flow into both variants via CSS vars.
    # Theme catalog (Briefa v0.2):
    #   "dark"      — default editorial dark (KhueForge feel).
    #   "bright"    — newspaper editorial light.
    #   "corporate" — solid navy + cyan + amber accents (Meta-for-Business
    #                 sponsored-post look). Picked when content is brand /
    #                 finance / B2B-leaning.
    theme_variant: Literal["dark", "bright", "corporate"] = "dark"
    theme_primary: str = "#22d3ee"
    theme_accent: str = "#4ade80"
    theme_bg: str = "#0a0e1a"
    theme_highlight: str = "#ef4444"
    theme_gold: str = "#fbbf24"

    # ── Voice ──
    voice_name: str = DEFAULT_VOICE_NAME
    voice_gender: Literal["female", "male"] = "female"
    # Tool-level override applied AFTER the planner picks a voice by
    # content_type. ``channel_default`` keeps the planner's choice (current
    # behavior). ``always_male`` / ``always_female`` are fixed. ``auto_by_topic``
    # runs a keyword classifier — female when the topic looks women / children
    # focused, male otherwise (the KhuePrinter HF-mode pattern).
    voice_gender_policy: Literal[
        "channel_default", "always_male", "always_female", "auto_by_topic"
    ] = "channel_default"
    # ElevenLabs voice ID — empty = skip ElevenLabs, use Edge TTS only.
    elevenlabs_voice_id: str = ""
    # Google Cloud TTS (Chirp 3 HD) — API key auth + voice name.
    # Empty key = skip Google, fall through to ElevenLabs/Edge.
    google_tts_api_key: str = ""
    google_tts_voice: str = ""

    # ── Facebook Page posting (optional — empty = no FB posting wired in) ──
    fb_page_id: str = ""
    fb_page_token: str = ""

    # ── Hashtags per platform ──
    # FB de-prioritizes posts that look spammy/hashtag-heavy, so we default
    # OFF for FB and ON for TikTok (where hashtags drive discovery). Each
    # channel can flip either knob.
    hashtags_fb_enabled: bool = False
    hashtags_tiktok_enabled: bool = True

    # ── Promo outro (showcase mode) ──
    # When enabled, the composer appends a short brand line read by Edge TTS
    # at the end of the video. Lets a single host repo demonstrate the tools
    # without baking a fixed brand into the renderer — users who fork the
    # repo flip ``PROMO_ENABLED=false`` in the channel ``.env`` to ship plain
    # videos, or replace the text to point at their own channel.
    promo_enabled: bool = False
    promo_outro_text: str = ""
    promo_outro_voice: str = DEFAULT_VOICE_NAME_MALE
    # Default +35% — matches the NEWS content body rate. The promo
    # synth now uses the SAME provider chain as body scenes (Google
    # Chirp 3 HD primary, Edge TTS fallback) so the rate translates
    # identically and the outro sounds flush with the body. Channels
    # without a Google TTS key fall through to Edge; +35% on Edge
    # vi-VN male voice still reads cleanly.
    promo_outro_rate: str = "+35%"

    # ── VCM Autopilot (overnight render daemon) ──
    # When enabled, ``tools.vcm.autopilot.scheduler`` includes this channel
    # in its rotation. Each tick the scheduler picks the most-indebted
    # AUTOPILOT_ENABLED channel and renders one topic. See
    # ``tools/vcm/autopilot/`` for the daemon code.
    autopilot_enabled: bool = False
    # Target nightly volume per channel. Scheduler tries to render this
    # many before the morning LingoFeeder cutoff (~06:30 VN).
    autopilot_videos_per_night: int = 1
    # LEGACY (W6 era, dropped 2026-06-06): used to render dual-aspect
    # short+long. Now superseded by ``autopilot_duration_mode`` below,
    # which always renders a single 9:16 aspect at either short or long
    # duration. Field kept for backward compat with old channel envs.
    autopilot_render_modes: Literal["both", "short", "long"] = "long"
    # Single-render mode for the autopilot. ``"long"`` = 9:16 5-7 min
    # script (publishes to FB Reels / TikTok / YouTube — all 3 cap above
    # 60 min, so length is not a problem). ``"short"`` = 9:16 75-90 s
    # script for fast pumps. Defaults to long because that's anh's
    # June 2026 decision after watching the dual-aspect render burn ~25 m
    # per topic; a single 9:16 long-form is ~12-15 m on the 1050 Ti.
    autopilot_duration_mode: Literal["short", "long"] = "long"
    # AI cartoon vs Pexels real photo. Stays per-aspect (both renders use
    # the same source). ``"ai"`` (default) routes through Cloudflare
    # Workers AI → Pollinations → Pexels.
    autopilot_image_source: Literal["ai", "stock"] = "ai"
    # Free-form suffix appended to every image-gen prompt — drives the
    # channel's visual personality without needing a new theme variant.
    # Example: "cosmic atmosphere, ethereal nebula lighting, sci-fi
    # cinematic". Empty = use composer's default cartoon style.
    autopilot_image_style_suffix: str = ""
    # One-paragraph description of the channel's niche / tone — fed to
    # Gemini at ideation time so generated topics match the brand.
    autopilot_niche_guide: str = ""
    # Pipe-separated seed topics. Used as tone anchors at ideation time;
    # the seeder MAY use them verbatim on the very first runs (when
    # history is empty), but normally Gemini drifts to fresh phrasings.
    autopilot_seed_topics: str = ""
    # Telegram chat to notify on render success. Positive int = user
    # DM, negative int (e.g. ``-100xxxxxxxxxx``) = group / supergroup.
    # 0 = no notification (autopilot still runs + writes URLs to disk).
    autopilot_notify_chat_id: int = 0


def load_channel_config(slug: str, channels_root: Path | str = "channels") -> ChannelConfig:
    """Load ``channels/<slug>/channel.env`` into a ``ChannelConfig``.

    Two files are read in order (later overrides earlier):

      1. ``channel.env``    — committed template (brand styling, voice, theme).
      2. ``.env.local``     — gitignored per-machine secrets (FB tokens, etc.).

    Both are optional. Missing channel directory raises ``FileNotFoundError``.
    """
    from dotenv import dotenv_values  # local import — dotenv is optional at top level

    channels_root = Path(channels_root)
    channel_dir = channels_root / slug
    if not channel_dir.is_dir():
        raise FileNotFoundError(f"Channel directory not found: {channel_dir}")

    raw: dict[str, str] = {}
    for fname in ("channel.env", ".env.local"):
        env_file = channel_dir / fname
        if env_file.is_file():
            for k, v in dotenv_values(env_file).items():
                if v is not None:
                    raw[k.lower()] = v
    return ChannelConfig.model_validate(raw)


__all__ = ["ChannelConfig", "load_channel_config"]
