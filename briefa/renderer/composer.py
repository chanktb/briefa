"""Scene plan → HyperFrames project at ``job_dir``.

Pipeline:
  1. Validate each scene's slots against its layout's slot model.
  2. Auto-resolve assets (HeroCardWithLogo logo, ScreenshotEmbed images).
  3. Optionally inject an "iPhone reader" scene for text-only input.
  4. Synthesize TTS audio per scene into ``assets/audio/scene_N.mp3``.
  5. Measure each MP3 to get exact audio duration.
  6. Compute per-scene duration = audio + small buffer (clamped to layout max).
  7. Cursor-walk to assign scene start times and total duration.
  8. Render ``composition.html.j2`` into ``index.html``.
  9. Emit supporting HyperFrames project files + manifest.json.

The renderer is the only module that knows about Jinja2 templates and the
HyperFrames project layout. TTS, scene models, and channel config live in
their own packages and are composed here.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from briefa.channel import ChannelConfig
from briefa.planner.content_types import (
    DEFAULT_VOICE_NAME_MALE,
    GOOGLE_TTS_POOL_FEMALE,
    GOOGLE_TTS_POOL_MALE,
)
from briefa.planner.fb_caption_editor import edit_fb_caption
from briefa.planner.scene_models import (
    LAYOUT_DURATIONS,
    LAYOUT_SLOTS_MODEL,
    LayoutId,
    Scene,
    ScenePlan,
    ScreenshotEmbedSlots,
)
from briefa.planner.voice_policy import apply_voice_gender_policy
from briefa.sources.url_extract import friendly_source_name
from briefa.tts.phonetic import apply_en_phonetics
from briefa.tts.router import generate_voice
from briefa.utils.audio_measure import measure_audio_duration

from .hyperframes import (
    HYPERFRAMES_VERSION_DEFAULT,
    write_project_files,
)

logger = logging.getLogger("briefa.renderer.composer")

# Tempo floor — all scenes run at max(plan_rate, this) so the video feels
# uniform across content types instead of dragging in TECH / LEARNING.
# Matches upstream ktb-studio: +25% is the floor at which Vietnamese
# voices stay snappy without sounding rushed.
_UNIFORM_RATE_FLOOR = 25  # percent

# Throttle between scene TTS calls — Microsoft Edge TTS rate-limits a
# single voice; the threshold has tightened in 2026 well past what
# lingo-feeder's 250 ms used to clear. CEO's news renders at 250 ms
# still saw 3-4 retries per scene (25s waste each).
#
# 2.0 s base + up to 1.0 s jitter clears the limit on every scene in
# practice. 6-scene render adds ~12 s wall-clock — negligible compared
# to the 60-120 s HyperFrames render that follows AND much faster than
# the 100+ s the retry path was burning before.
_PER_SCENE_TTS_THROTTLE_SECONDS = 2.0
_PER_SCENE_TTS_THROTTLE_JITTER = 1.0

# Small buffer added to each scene's audio_duration so the fade-out has room
# without leaving dead air between scenes.
_SCENE_BUFFER_SECONDS = 0.3

LAYOUT_FILENAMES: dict[LayoutId, str] = {
    LayoutId.TITLE_HERO:           "title_hero.html.j2",
    LayoutId.BULLET_LIST:          "bullet_list.html.j2",
    LayoutId.KPI_GRID:             "kpi_grid.html.j2",
    LayoutId.TIMELINE:             "timeline.html.j2",
    LayoutId.CTA_OUTRO:            "cta_outro.html.j2",
    LayoutId.HERO_CARD_WITH_LOGO:  "hero_card_with_logo.html.j2",
    LayoutId.BIG_STAT_CARD:        "big_stat_card.html.j2",
    LayoutId.TERMINAL_WINDOW:      "terminal_window.html.j2",
    LayoutId.SCREENSHOT_EMBED:     "screenshot_embed.html.j2",
}

# Asset directories live alongside this module.
_RENDERER_DIR = Path(__file__).resolve().parent
_LAYOUTS_DIR = _RENDERER_DIR / "layouts"


# ════════════════════════════════════════════════════════════════════════
# Jinja environment
# ════════════════════════════════════════════════════════════════════════

def _highlight_filter(text: str, word: str, klass: str = "highlight") -> str:
    """Wrap the first occurrence of ``word`` with ``<span class="klass">``."""
    if not text or not word or word not in text:
        return text
    return text.replace(word, f"<span class='{klass}'>{word}</span>", 1)


def _build_jinja_env() -> Environment:
    """Jinja env that resolves ``composition.html.j2`` + ``theme.css`` next to
    this module and ``layouts/<file>.html.j2`` under ``layouts/``."""
    loader = FileSystemLoader([str(_RENDERER_DIR), str(_RENDERER_DIR / "layouts"), str(_RENDERER_DIR)])
    env = Environment(
        loader=loader,
        autoescape=select_autoescape(enabled_extensions=()),  # we emit raw HTML
        trim_blocks=False,
        lstrip_blocks=False,
    )
    env.filters["highlight"] = lambda t, w: _highlight_filter(t, w, "highlight")
    env.filters["highlight_hot"] = lambda t, w: _highlight_filter(t, w, "highlight-hot")
    return env


# ════════════════════════════════════════════════════════════════════════
# Subtitle + bullet karaoke timing
# ════════════════════════════════════════════════════════════════════════

def _bold_first_keyword(sentence: str) -> str:
    """Wrap the first word ≥5 chars in ``<span class='hl'>`` for emphasis."""
    words = sentence.split()
    for i, w in enumerate(words):
        cleaned = w.strip(".,!?;:")
        if len(cleaned) >= 5:
            words[i] = f"<span class='hl'>{w}</span>"
            break
    return " ".join(words)


def compute_bullet_timings(scene: Scene) -> list[float]:
    """When a BulletList scene's voice has N+1 sentences (intro + N bullets),
    weight each bullet's reveal time by its sentence's character count so the
    karaoke highlight tracks the actual voice instead of drifting on intro
    length. Falls back to even-split when the sentence count doesn't match.
    """
    bullets = getattr(scene.slots, "bullets", None) or []
    n_bullets = len(bullets)
    if n_bullets == 0:
        return []
    audio_dur = float(
        getattr(scene, "audio_duration", 0.0)
        or getattr(scene, "duration", 0.0)
        or 10.0
    )
    sentences = [
        s.strip() for s in re.split(r"(?<=[.!?])\s+", scene.voice_script) if s.strip()
    ]
    karaoke_lag = 0.25
    tail_reserve = 0.3

    if len(sentences) == n_bullets + 1:
        char_counts = [max(len(s), 1) for s in sentences]
        total_chars = sum(char_counts)
        usable = max(audio_dur - tail_reserve, n_bullets * 1.0)
        cum = 0.0
        times: list[float] = []
        for i, c in enumerate(char_counts):
            if i > 0:
                times.append(round(min(cum + karaoke_lag, usable - 0.1), 2))
            cum += (c / total_chars) * usable
        return times

    intro_buffer = 1.5
    usable = max(audio_dur - intro_buffer - tail_reserve, n_bullets * 1.0)
    per = usable / n_bullets
    return [round(intro_buffer + i * per + karaoke_lag, 2) for i in range(n_bullets)]


def split_voice_into_segments(scene: Scene) -> list[dict[str, Any]]:
    """Split ``voice_script`` into sentences spread evenly across the audio.

    Time is divided by the spoken audio duration, not the scene's display
    duration: when the promo logic later extends the last scene to cover the
    outro, the per-scene subtitles still finish on the audio boundary so the
    promo subtitle can take over without a stale sentence flashing back over
    it. Falls back to ``scene.duration`` when ``audio_duration`` hasn't been
    measured yet (rare, but possible in tests).
    """
    start = float(getattr(scene, "start", 0.0))
    audio_dur = float(getattr(scene, "audio_duration", 0.0))
    if audio_dur <= 0:
        audio_dur = float(getattr(scene, "duration", 0.0))
    sentences = [
        s.strip() for s in re.split(r"(?<=[.!?])\s+", scene.voice_script) if s.strip()
    ]
    if not sentences:
        return [{"t": round(start, 2), "text": scene.voice_script}]
    slot = audio_dur / max(len(sentences), 1)
    segments = [
        {"t": round(start + i * slot, 2), "text": _bold_first_keyword(sent)}
        for i, sent in enumerate(sentences)
    ]
    segments.append({"t": round(start + audio_dur, 2), "text": ""})
    return segments


# ════════════════════════════════════════════════════════════════════════
# Slot validation
# ════════════════════════════════════════════════════════════════════════

def _validate_slots(scene: Scene) -> None:
    """Validate ``scene.slots`` against the layout's Pydantic model.

    Mutates ``scene.slots`` to the validated Pydantic instance so templates
    can use attribute access freely.
    """
    model_cls = LAYOUT_SLOTS_MODEL.get(scene.layout_id)
    if model_cls is None:
        raise ValueError(f"Unknown layout_id: {scene.layout_id}")
    if not hasattr(scene.slots, "model_dump"):  # raw dict from planner
        scene.slots = model_cls.model_validate(scene.slots)


# ════════════════════════════════════════════════════════════════════════
# ScreenshotEmbed asset resolver
# ════════════════════════════════════════════════════════════════════════

def _download_image(url: str, out_path: Path) -> bool:
    """Best-effort sync HTTP download. Never raises."""
    try:
        import httpx
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            r = client.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 ktb-news-editor/0.1"},
            )
        if r.status_code != 200 or not r.content:
            return False
        out_path.write_bytes(r.content)
        return True
    except Exception as exc:
        logger.debug("image download failed for %s: %s", url, exc)
        return False


def _resolve_screenshot_assets(
    scene_plan: ScenePlan,
    source_meta: dict,
    job_dir: Path,
) -> None:
    """For each ``ScreenshotEmbed`` scene, fill ``scene.image_urls`` with
    job-local paths after copying / downloading source images.

    Sources, in priority order:
      - source_meta["source_image"]              → ``og_image.jpg``
      - source_meta["source_images"][:5]         → ``article_img_N.jpg``
      - source_meta["photo_paths"][:5]           → ``photo_N.<ext>`` (user uploads)

    Variant → image pool:
      - browser / highlight / iphone : URL pool round-robin
      - minimal                       : first user photo (or first URL)
      - stack                         : 3 user photos (or 3 URL images)

    When no images are available, ScreenshotEmbed scenes are demoted to the
    ``iphone`` variant with a synthesized ``article_body`` so the scene
    becomes an "iPhone reader" instead of an empty fallback.
    """
    static_dir = job_dir / "static"
    static_dir.mkdir(parents=True, exist_ok=True)

    url_pool: list[str] = []
    source_image = source_meta.get("source_image", "")
    if source_image:
        og_path = static_dir / "og_image.jpg"
        if og_path.exists() or _download_image(source_image, og_path):
            url_pool.append("static/og_image.jpg")

    for i, url in enumerate(list(source_meta.get("source_images") or [])[:5], start=1):
        path = static_dir / f"article_img_{i}.jpg"
        if path.exists() or _download_image(url, path):
            url_pool.append(f"static/article_img_{i}.jpg")

    photo_locals: list[str] = []
    for i, src in enumerate(list(source_meta.get("photo_paths") or [])[:5], start=1):
        try:
            src_p = Path(src)
            if not src_p.is_file():
                continue
            ext = src_p.suffix.lower() or ".jpg"
            dst = static_dir / f"photo_{i}{ext}"
            shutil.copy2(src_p, dst)
            photo_locals.append(f"static/photo_{i}{ext}")
        except OSError as exc:
            logger.debug("photo copy failed: %s", exc)

    no_images_mode = not url_pool and not photo_locals
    url_idx = 0
    for scene in scene_plan.scenes:
        if scene.layout_id is not LayoutId.SCREENSHOT_EMBED:
            continue
        slots = scene.slots
        # _resolve_screenshot_assets runs after _validate_slots, so slots is
        # always a ScreenshotEmbedSlots instance here.
        if not isinstance(slots, ScreenshotEmbedSlots):
            continue
        variant = slots.variant

        if no_images_mode:
            try:
                # CEO 2026-06-14: switched no-images fallback from "iphone"
                # to "mac_reader" — wider article column, no phone tells.
                slots.variant = "mac_reader"
                variant = "mac_reader"
                if not slots.article_body:
                    slots.article_body = _synthesize_article_body(scene.voice_script)
                if not slots.article_headline:
                    slots.article_headline = (slots.section_title or scene_plan.title)[:90]
            except Exception as exc:
                logger.debug("no-images fallback failed: %s", exc)

        if variant in ("browser", "highlight", "iphone", "mac_reader"):
            if url_pool:
                urls = [url_pool[url_idx % len(url_pool)]]
                url_idx += 1
            else:
                urls = []
        elif variant == "minimal":
            urls = [photo_locals[0]] if photo_locals else ([url_pool[0]] if url_pool else [])
        elif variant == "stack":
            if photo_locals:
                pool = photo_locals[:3]
            elif url_pool:
                pool = url_pool[:3]
            else:
                pool = []
            while pool and len(pool) < 3:
                pool.append(pool[-1])
            urls = pool
        else:
            urls = []

        scene.image_urls = urls  # type: ignore[attr-defined]


# ════════════════════════════════════════════════════════════════════════
# Voice selection per channel + content type
# ════════════════════════════════════════════════════════════════════════

def _resolve_google_voice(scene_plan: ScenePlan, channel_config: ChannelConfig) -> str:
    """Pick a Google Chirp HD voice for this plan.

    Priority:
      1. ``channel.google_tts_voice`` if explicitly set (caller override).
      2. Derived from ``scene_plan.voice_name`` gender → male / female pool.
      3. Stable random within the pool keyed by ``plan.title`` so repeated
         renders of the same plan keep the same voice.

    Returns an empty string when Google isn't configured for this channel.
    """
    if channel_config.google_tts_voice:
        return channel_config.google_tts_voice
    if not channel_config.google_tts_api_key:
        return ""
    is_male = scene_plan.voice_name == DEFAULT_VOICE_NAME_MALE
    pool = GOOGLE_TTS_POOL_MALE if is_male else GOOGLE_TTS_POOL_FEMALE
    seed = abs(hash(scene_plan.title or scene_plan.channel)) % len(pool)
    return pool[seed]


# ════════════════════════════════════════════════════════════════════════
# iPhone reader scene injection (text-only input fallback)
# ════════════════════════════════════════════════════════════════════════

def _synthesize_article_body(voice_script: str) -> str:
    """Convert ``voice_script`` to 2–3 short ``<p>`` paragraphs for the
    iPhone-reader ScreenshotEmbed body. HTML markup overhead caps the
    output at ~1150 chars so it fits ``article_body``'s 1200-char limit.

    Defensive strip in case ``voice_script`` still carries dossier markers.
    """
    voice_script = _strip_dossier_leakage(voice_script)
    if not voice_script:
        return ""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", voice_script) if s.strip()]
    if not sentences:
        return ""
    n = len(sentences)
    if n <= 2:
        paras = [sentences]
    elif n <= 4:
        mid = (n + 1) // 2
        paras = [sentences[:mid], sentences[mid:]]
    else:
        a, b = n // 3, 2 * n // 3
        paras = [sentences[:a], sentences[a:b], sentences[b:]]

    body_cap = 1150
    html_parts: list[str] = []
    total = 0
    for para in paras:
        text = " ".join(para)
        if total + len(text) > body_cap:
            text = text[: max(body_cap - total, 0)].rstrip() + "…"
        text = _bold_first_keyword(text)
        html_parts.append(f"<p>{text}</p>")
        total += len(text)
        if total >= body_cap:
            break
    return "".join(html_parts)


def _extract_voice_intro_from_text(original_text: str) -> str:
    """Pick the first 1–3 sentences of ``original_text`` as the voice script
    for the injected iPhone reader scene. Output stays inside the Scene
    model's ``voice_script`` length bounds (80 ≤ len ≤ 450).

    Defensive against ``original_text`` still carrying dossier scaffolding —
    if pipeline.py was bypassed (e.g. a future caller hands us a raw
    dossier) we still strip the markers here.
    """
    original_text = _strip_dossier_leakage(original_text)
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", original_text) if s.strip()]
    if not sentences:
        text = original_text.strip()
        if len(text) >= 80:
            return text[:280] + ("…" if len(text) > 280 else "")
        return text + " " * (80 - len(text))

    accumulated: list[str] = []
    total = 0
    for sent in sentences:
        accumulated.append(sent)
        total += len(sent) + 1
        if total >= 120:
            break
    text = " ".join(accumulated).strip()
    if len(text) > 380:
        cut = text[:380]
        last_space = cut.rfind(" ")
        text = (cut[:last_space] if last_space > 200 else cut).rstrip(",.;:") + "…"
    if len(text) < 80:
        for sent in sentences[len(accumulated):]:
            text = text + " " + sent
            if len(text) >= 80:
                break
        if len(text) < 80:
            text = text + " Nội dung chi tiết hiện trên màn hình anh chị có thể xem qua."
    return text[:445]


def _maybe_inject_iphone_reader_scene(
    scene_plan: ScenePlan,
    source_meta: dict,
    channel_config: ChannelConfig,
) -> bool:
    """Inject a ``ScreenshotEmbed/iphone`` scene that renders the original
    input text as a phone article preview. Only fires for text-only input
    (no images, no source URL with og:image) when the planner hasn't already
    emitted a ScreenshotEmbed scene.
    """
    original_text = (source_meta.get("original_text") or "").strip()
    if not original_text or len(original_text) < 60:
        return False
    if source_meta.get("photo_paths") or source_meta.get("source_image"):
        return False
    if source_meta.get("source_images"):
        return False
    if any(s.layout_id is LayoutId.SCREENSHOT_EMBED for s in scene_plan.scenes):
        return False

    body_html = _synthesize_article_body(original_text)
    if not body_html:
        return False

    headline = (scene_plan.title or "Nội dung bài viết")[:90]
    domain = (
        source_meta.get("source_domain")
        or (channel_config.channel_handle.lstrip("@") if channel_config.channel_handle else "")
        or scene_plan.channel
    )
    byline = (
        f"{channel_config.channel_name} · vừa xong"
        if channel_config.channel_name
        else "vừa xong"
    )

    # CEO 2026-06-14: iPhone reader screenshot was visually weak (status bar
    # tells, Dynamic Island, narrow article column). Swapped for a MacBook
    # browser window variant — wider article column, cleaner chrome (3 dots
    # + URL bar), more "publication" feel.
    se_slots = ScreenshotEmbedSlots(
        variant="mac_reader",
        section_title="",
        highlight_word="",
        display_url=domain[:80],
        caption="",
        stat_big="",
        stat_text="",
        filename="",
        total_photo_count=1,
        article_category="NỘI DUNG GỐC",
        article_headline=headline,
        article_byline=byline,
        article_pullquote="",
        article_body=body_html,
    )

    new_scene = Scene(
        scene_index=2,
        layout_id=LayoutId.SCREENSHOT_EMBED,
        slots=se_slots,
        voice_script=_extract_voice_intro_from_text(original_text),
    )
    scene_plan.scenes.insert(1, new_scene)
    for i, s in enumerate(scene_plan.scenes, start=1):
        s.scene_index = i
    return True


# ════════════════════════════════════════════════════════════════════════
# Channel static asset copy
# ════════════════════════════════════════════════════════════════════════

def _copy_channel_avatar(
    channel_config: ChannelConfig,
    channel_slug: str,
    channels_root: Path,
    job_dir: Path,
) -> ChannelConfig:
    """Copy ``channels/<slug>/static/<avatar>`` to ``job_dir/static/`` so the
    composition template can reference it as a relative URL.

    Returns the (possibly modified) ``ChannelConfig``: if the declared
    avatar file is missing on disk, ``avatar_filename`` is cleared so the
    template falls back to the chip-dot animation instead of a broken img.
    """
    if not channel_config.avatar_filename:
        return channel_config

    source_path = channels_root / channel_slug / "static" / channel_config.avatar_filename
    if not source_path.exists():
        logger.info("avatar file missing, clearing: %s", source_path)
        return channel_config.model_copy(update={"avatar_filename": ""})

    dest_dir = job_dir / "static"
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, dest_dir / channel_config.avatar_filename)
    return channel_config


# ════════════════════════════════════════════════════════════════════════
# FB caption builder (manifest output)
# ════════════════════════════════════════════════════════════════════════

async def _build_captions(
    scene_plan: ScenePlan,
    source_meta: dict,
    channel_config: ChannelConfig,
) -> tuple[str, str, str]:
    """Build ``(fb_caption, fb_first_comment, tiktok_caption)``.

    Anh's caption rules (2026-06-06 revision):

    1. The caption is the FULL post — Gemini rewrites the per-scene
       voice-over transcripts into a long, scroll-friendly Facebook
       post with a hook, rotating-icon paragraph headers, and a closing
       line. See :func:`core.planner.fb_caption_editor.edit_fb_caption`.

    2. When a source URL is present:
         - caption ends with the line "🔗 URL nguồn ở comment 👇"
         - ``fb_first_comment`` holds ONLY the URL (FB Graph API posts it
           as the first comment after the photo / video upload).

    3. When there is no source URL (e.g. autopilot-generated topic):
         - caption ends with the regular wrap-up only.
         - ``fb_first_comment`` is empty.

    4. TikTok caption = identical text to FB. TikTok doesn't support
       auto-posting a follow-up comment, so the "URL nguồn ở comment"
       line just dangles there — anh said "cap giống nhau luôn" so
       we don't fork the wording.

    Hashtags follow per-platform channel toggles:
      - ``hashtags_fb_enabled``     (default ``False``)
      - ``hashtags_tiktok_enabled`` (default ``True``)

    Field name kept as ``fb_caption_short`` in the manifest for
    backwards compatibility with poster code that already reads it,
    even though the content is no longer "short".
    """
    source_url = source_meta.get("source_url") or ""

    first_scene = scene_plan.scenes[0] if scene_plan.scenes else None
    hashtag_words: list[str] = []
    if first_scene is not None:
        chips = getattr(first_scene.slots, "tag_chips", None)
        if isinstance(chips, list):
            hashtag_words = list(chips)[:3]
    hashtags = ""
    if hashtag_words:
        hashtags = " ".join(
            "#" + re.sub(r"[^a-zA-Z0-9À-ỹ]", "", w.replace(" ", ""))
            for w in hashtag_words if w
        ).strip()

    fb_tags = hashtags if channel_config.hashtags_fb_enabled else ""
    tt_tags = hashtags if channel_config.hashtags_tiktok_enabled else ""

    # Gemini rewrites voice-over scripts → FB long-form post. Falls back
    # to a simple rotating-icon assembly if Gemini is unavailable; never
    # raises (caption editor catches everything).
    edited = await edit_fb_caption(scene_plan)

    # Tail line — only when we actually have a URL to drop in the
    # first comment. No URL = no "look in comments" line (would be
    # misleading).
    source_line = "🔗 URL nguồn ở comment 👇" if source_url else ""

    def _assemble(body: str, tags: str) -> str:
        parts = [body]
        if source_line:
            parts.extend(["", source_line])
        if tags:
            parts.extend(["", tags])
        return "\n".join(parts).strip()

    fb_caption = _assemble(edited, fb_tags)
    tiktok_caption = _assemble(edited, tt_tags)

    # First comment payload — just the URL. The old version dumped the
    # whole transcript here; anh switched to a leaner comment that's
    # purely the click-through.
    fb_first_comment = source_url.strip() if source_url else ""

    return fb_caption, fb_first_comment, tiktok_caption


# ════════════════════════════════════════════════════════════════════════
# Promo outro (channel-driven showcase line)
# ════════════════════════════════════════════════════════════════════════

@dataclass
class _PromoResult:
    """Promo synth output. Empty ``audio_url`` means no promo was generated."""
    audio_url: str = ""
    start: float = 0.0
    duration: float = 0.0


async def _maybe_synth_promo(
    channel_config: ChannelConfig,
    scene_plan: ScenePlan,
    job_dir: Path,
) -> _PromoResult:
    """Synthesize the promo outro when the channel opts in.

    Promo goes through the SAME provider chain as the body scenes
    (Google Chirp 3 HD → Edge TTS → gTTS). Earlier this was hard-coded
    to Edge TTS "for brand voice consistency", but the side effect was
    a perceived slowdown at the outro: a NEWS body at Google
    speakingRate 1.35 sounds noticeably faster than Edge TTS at the
    same +35% rate, so the CTA felt like it was crawling.

    Sticking with the channel's main ``google_tts_voice`` keeps the
    brand voice consistent within a channel (it's the same fixed voice
    every render) while matching the body's pacing. ElevenLabs is
    still explicitly skipped — its quota is small and a single
    long video already burns the body scenes' budget.
    """
    if not channel_config.promo_enabled or not channel_config.promo_outro_text.strip():
        return _PromoResult()
    if not scene_plan.scenes:
        return _PromoResult()

    promo_mp3 = job_dir / "assets" / "audio" / "promo_outro.mp3"
    promo_text = apply_en_phonetics(channel_config.promo_outro_text)
    try:
        await generate_voice(
            text=promo_text,
            voice_name=channel_config.promo_outro_voice,  # Edge fallback name
            rate=channel_config.promo_outro_rate,
            out_path=promo_mp3,
            elevenlabs_voice_id="",  # still skip ElevenLabs
            # Pass the channel's Google Chirp creds so promo uses the
            # same provider as body scenes. Empty = falls through to
            # Edge automatically (router behaviour).
            google_tts_api_key=channel_config.google_tts_api_key,
            google_tts_voice=channel_config.google_tts_voice,
        )
    except Exception as exc:
        logger.warning("promo TTS failed, skipping outro: %s", exc)
        return _PromoResult()

    last_scene = scene_plan.scenes[-1]
    promo_start = round(last_scene.start + last_scene.audio_duration + 0.15, 2)
    promo_duration = round(measure_audio_duration(promo_mp3), 2)
    return _PromoResult(
        audio_url="assets/audio/promo_outro.mp3",
        start=promo_start,
        duration=promo_duration,
    )


# ════════════════════════════════════════════════════════════════════════
# Citation chip binding (Briefa Phase 3, see DECISIONS D005)
# ════════════════════════════════════════════════════════════════════════


_DOSSIER_LEAK_PATTERNS = [
    re.compile(r"═══+"),
    re.compile(r"BRIEFA_MULTI_SOURCE[^\n]*", re.IGNORECASE),
    re.compile(r"FETCH_FAILURES[^\n]*", re.IGNORECASE),
    re.compile(r"\[SOURCE\s+\d+\][^\n]*"),
    re.compile(r"^\s*URL:\s+\S+\s*$", re.MULTILINE),
    re.compile(r"^\s*---\s*$", re.MULTILINE),
    re.compile(
        r"Each scene in the output MUST cite ONE source[^\n]*",
        re.IGNORECASE,
    ),
    re.compile(
        r"Set citation_source_index\s*=\s*n[^\n]*",
        re.IGNORECASE,
    ),
    re.compile(
        r"DO NOT mix facts across sources.*?voice_script\.",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"without making the attribution explicit[^\n]*",
        re.IGNORECASE,
    ),
    re.compile(
        r"Do not invent content for these[^\n]*",
        re.IGNORECASE,
    ),
    # Header line types Gemini sometimes copies wholesale.
    re.compile(r"USER TEXT\s*·\s*User notes[^\n]*", re.IGNORECASE),
]


def _strip_dossier_leakage(text: str) -> str:
    """Remove dossier prompt-scaffolding patterns Gemini sometimes copies.

    Defense in depth on top of the SYSTEM_PROMPT_VI literal ban: even when
    the planner ignores the rule we never ship a video whose voice reads
    "BRIEFA_MULTI_SOURCE — 1 ingested source(s)..." aloud.

    Returns the cleaned text with the scaffolding stripped. Multiple
    consecutive blank lines collapsed to a single blank.
    """
    if not text:
        return text
    cleaned = text
    for pat in _DOSSIER_LEAK_PATTERNS:
        cleaned = pat.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _scrub_plan_dossier_leakage(scene_plan: ScenePlan) -> None:
    """Walk every scene + slot and strip dossier scaffolding in-place.

    Logged at INFO when anything was actually removed so the regression
    is visible in worker logs without dumping full text.
    """
    changes = 0
    for scene in scene_plan.scenes:
        original = scene.voice_script or ""
        cleaned = _strip_dossier_leakage(original)
        if cleaned != original:
            scene.voice_script = cleaned
            changes += 1
        # Walk slot text fields that the iPhone reader / BulletList / etc.
        # render verbatim. We only touch string-typed slot values.
        slots = scene.slots
        if hasattr(slots, "model_dump"):
            slot_dict = slots.model_dump()
        elif isinstance(slots, dict):
            slot_dict = slots
        else:
            continue
        for k, v in list(slot_dict.items()):
            if isinstance(v, str):
                v2 = _strip_dossier_leakage(v)
                if v2 != v:
                    if hasattr(slots, k):
                        try:
                            setattr(slots, k, v2)
                        except Exception:
                            pass
                    elif isinstance(slots, dict):
                        slots[k] = v2
                    changes += 1
    if changes:
        logger.warning(
            "scrubbed %d dossier-leak token(s) from scene plan — "
            "Gemini ignored SYSTEM_PROMPT rule A.1",
            changes,
        )


def _bind_citation_chips(scene_plan: ScenePlan, source_meta: dict | None) -> None:
    """Bind ``Scene.citation_source_index`` → ``citation_name/domain/url``.

    The planner emits a 1-based index into the router's ok-sources list (see
    ``briefa.sources.router.route_and_merge``). At render time we resolve that
    index to the friendly chip data so the Jinja template can emit one chip
    overlay per scene.

    Behaviour:

      * idx == 0       → no chip (e.g. CTAOutro brand row).
      * 1 ≤ idx ≤ N    → bind chip from ``source_meta['sources'][idx-1]``.
      * idx > N        → fall back to the primary source (idx == 1) and log a
        warning. Defensive against a planner output that points past the end
        when sources fail mid-flight.
      * idx unset AND a single source ingested → auto-bind to source 1 so the
        user still sees provenance.
    """
    meta = source_meta or {}
    raw_sources = meta.get("sources") or []
    ok_sources = [s for s in raw_sources if s.get("ok")]
    if not ok_sources:
        return

    auto_bind_single = len(ok_sources) == 1

    for scene in scene_plan.scenes:
        if scene.layout_id is LayoutId.CTA_OUTRO:
            continue
        idx = int(getattr(scene, "citation_source_index", 0) or 0)
        if idx == 0 and auto_bind_single:
            idx = 1
        if idx <= 0:
            continue
        if idx > len(ok_sources):
            logger.warning(
                "citation_source_index=%d out of range (have %d) on scene %d — "
                "falling back to source 1",
                idx, len(ok_sources), scene.scene_index,
            )
            idx = 1
        cit = (ok_sources[idx - 1].get("citation") or {})
        scene.citation_name = cit.get("name", "")
        scene.citation_domain = cit.get("domain", "")
        scene.citation_url = cit.get("url", "")
        scene.citation_source_index = idx


# ════════════════════════════════════════════════════════════════════════
# Main compose() entry point
# ════════════════════════════════════════════════════════════════════════

async def compose(
    scene_plan: ScenePlan,
    channel_config: ChannelConfig,
    job_dir: Path,
    *,
    channels_root: Path | str = "channels",
    hyperframes_version: str = HYPERFRAMES_VERSION_DEFAULT,
    source_meta: dict | None = None,
) -> Path:
    """Build a complete HyperFrames project at ``job_dir``.

    Args:
        scene_plan:           Output of the planner, already validated end-to-end.
        channel_config:       Brand preset loaded from ``channels/<slug>/.env``.
        job_dir:              Destination directory (created if missing).
        channels_root:        Where ``channels/<slug>/static/avatar.jpg`` is read from.
        hyperframes_version:  Pinned HyperFrames CLI version.
        source_meta:          Optional input metadata — source URL/domain/image,
                              user photo paths, original text, GitHub stats,
                              and ``add_promo`` flag.

    Returns:
        Path to the rendered ``index.html``.
    """
    source_meta = source_meta or {}
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "assets" / "audio").mkdir(parents=True, exist_ok=True)
    channels_root = Path(channels_root)

    # ── Channel avatar copy ──
    channel_config = _copy_channel_avatar(
        channel_config, scene_plan.channel, channels_root, job_dir,
    )

    # ── Channel voice-gender policy ──
    # Applied BEFORE TTS so Google Chirp voice resolution (which derives
    # gender from ``scene_plan.voice_name``) picks the right pool.
    apply_voice_gender_policy(scene_plan, channel_config.voice_gender_policy)

    # ── Auto-fill HeroCardWithLogo.logo_url from source_meta ──
    avatar_url = ""
    gh_stats = source_meta.get("github_stats") or {}
    if gh_stats.get("owner_avatar_url"):
        avatar_url = gh_stats["owner_avatar_url"]
    elif source_meta.get("source_image"):
        avatar_url = source_meta["source_image"]
    if avatar_url:
        for scene in scene_plan.scenes:
            if scene.layout_id is LayoutId.HERO_CARD_WITH_LOGO:
                slots = scene.slots
                if isinstance(slots, dict):
                    if not slots.get("logo_url"):
                        slots["logo_url"] = avatar_url
                elif not getattr(slots, "logo_url", ""):
                    with contextlib.suppress(Exception):
                        slots.logo_url = avatar_url  # type: ignore[attr-defined]

    # ── Inject iPhone reader scene for text-only input ──
    try:
        if _maybe_inject_iphone_reader_scene(scene_plan, source_meta, channel_config):
            logger.info("iPhone reader scene injected (text-only input)")
    except Exception as exc:
        logger.warning("Skip iPhone reader inject: %s", exc)

    # ── Scrub dossier scaffolding from planner output (BUG fix 2026-06-14) ──
    # Strips any "BRIEFA_MULTI_SOURCE", "[SOURCE n]", "═══", instruction
    # prose Gemini may have copied verbatim from the user-message dossier.
    _scrub_plan_dossier_leakage(scene_plan)

    # ── Bind citation chips (Briefa N-mix-input) ──
    # Resolves Scene.citation_source_index (planner-set, 1-based into the
    # router's ok-sources list) into runtime fields citation_name/domain/url
    # the Jinja template renders as a bottom overlay chip per scene.
    _bind_citation_chips(scene_plan, source_meta)

    # ── Validate slots per layout ──
    for scene in scene_plan.scenes:
        _validate_slots(scene)

    # ── Resolve ScreenshotEmbed image assets ──
    _resolve_screenshot_assets(scene_plan, source_meta, job_dir)

    # ── TTS per scene ──
    try:
        base_rate = int(scene_plan.voice_rate.replace("+", "").replace("%", "").strip() or "0")
    except ValueError:
        base_rate = 0
    uniform_rate = f"+{max(base_rate, _UNIFORM_RATE_FLOOR)}%"
    google_voice_chosen = _resolve_google_voice(scene_plan, channel_config)
    if channel_config.google_tts_api_key:
        logger.info(
            "Google TTS voice for plan: %s (content_type=%s, edge_voice=%s)",
            google_voice_chosen, scene_plan.content_type.value, scene_plan.voice_name,
        )

    last_idx = len(scene_plan.scenes) - 1
    for i, scene in enumerate(scene_plan.scenes):
        mp3_path = job_dir / "assets" / "audio" / f"scene_{scene.scene_index}.mp3"
        voice_text = apply_en_phonetics(scene.voice_script)
        await generate_voice(
            text=voice_text,
            voice_name=scene_plan.voice_name,
            rate=uniform_rate,
            out_path=mp3_path,
            elevenlabs_voice_id=channel_config.elevenlabs_voice_id,
            google_tts_api_key=channel_config.google_tts_api_key,
            google_tts_voice=google_voice_chosen,
        )
        # Throttle between scene calls to stay inside Edge TTS's
        # per-voice rate window. Skip after the last scene — no
        # follow-up TTS for it to throttle against. Jitter prevents
        # Microsoft pattern-matching identical inter-call gaps.
        if i < last_idx:
            import random as _rnd
            wait = _PER_SCENE_TTS_THROTTLE_SECONDS + _rnd.uniform(
                0.0, _PER_SCENE_TTS_THROTTLE_JITTER,
            )
            await asyncio.sleep(wait)

    # ── Measure audio + bullet timing ──
    for scene in scene_plan.scenes:
        mp3_path = job_dir / "assets" / "audio" / f"scene_{scene.scene_index}.mp3"
        scene.audio_duration = round(measure_audio_duration(mp3_path), 2)
        if scene.layout_id is LayoutId.BULLET_LIST:
            scene.bullet_timings = compute_bullet_timings(scene)  # type: ignore[attr-defined]

    # ── Per-scene duration = audio + buffer, clamped to layout max ──
    for scene in scene_plan.scenes:
        bounds = LAYOUT_DURATIONS[scene.layout_id]
        scene.duration = round(min(scene.audio_duration + _SCENE_BUFFER_SECONDS, bounds["max"]), 2)

    # ── Cursor walk → start times + total ──
    cursor = 0.0
    for scene in scene_plan.scenes:
        scene.start = round(cursor, 2)
        cursor = round(cursor + scene.duration, 2)
    total_duration = round(cursor, 2)

    # ── Promo outro (if channel opts in) ──
    promo = await _maybe_synth_promo(channel_config, scene_plan, job_dir)
    if promo.audio_url:
        # Stretch the last scene so it stays on screen while the promo plays,
        # then bump the total duration to match. 0.3s tail gives the audio
        # a breath at the end instead of a hard cut.
        extension = promo.duration + 0.3
        last_scene = scene_plan.scenes[-1]
        last_scene.duration = round(last_scene.duration + extension, 2)
        total_duration = round(total_duration + extension, 2)
        logger.info(
            "promo outro: +%.2fs (audio=%.2fs) — total now %.2fs",
            extension, promo.duration, total_duration,
        )

    # ── Attach layout file names for the {% include %} dispatch ──
    for scene in scene_plan.scenes:
        scene.layout_filename = LAYOUT_FILENAMES[scene.layout_id]

    # ── Subtitle ticker segments ──
    subtitle_segments: list[dict[str, Any]] = []
    for scene in scene_plan.scenes:
        subtitle_segments.extend(split_voice_into_segments(scene))
    if promo.audio_url:
        # Subtitle during the promo span = the same line the voice is reading.
        # Highlight the brand name so the eye lands on it; fall back to the
        # raw text when "Khuê Trần" isn't present so a forked channel with
        # custom promo text still renders cleanly.
        promo_subtitle = channel_config.promo_outro_text.strip()
        if "Khuê Trần" in promo_subtitle:
            promo_subtitle = promo_subtitle.replace(
                "Khuê Trần", "<span class='hl'>Khuê Trần</span>", 1,
            )
        subtitle_segments.append({"t": promo.start, "text": promo_subtitle})
        subtitle_segments.append({
            "t": round(promo.start + promo.duration, 2),
            "text": "",
        })

    # ── Render composition.html.j2 ──
    env = _build_jinja_env()
    template = env.get_template("composition.html.j2")
    html = template.render(
        scenes=scene_plan.scenes,
        total_duration=total_duration,
        channel=channel_config,
        voice_name=scene_plan.voice_name,
        subtitle_segments=subtitle_segments,
        source_domain=source_meta.get("source_domain"),
        source_url=source_meta.get("source_url"),
        aspect_ratio=getattr(scene_plan, "aspect_ratio", "9:16"),
        promo_audio=promo.audio_url,
        promo_start=promo.start,
        promo_duration=promo.duration,
    )
    index_path = job_dir / "index.html"
    index_path.write_text(html, encoding="utf-8")

    # ── HyperFrames supporting files + manifest ──
    write_project_files(
        job_dir=job_dir,
        project_id=job_dir.name,
        title=scene_plan.title,
        total_duration=total_duration,
        hyperframes_version=hyperframes_version,
    )
    await _write_manifest(
        job_dir=job_dir,
        scene_plan=scene_plan,
        total_duration=total_duration,
        source_meta=source_meta,
        channel_config=channel_config,
    )

    return index_path


async def _write_manifest(
    *,
    job_dir: Path,
    scene_plan: ScenePlan,
    total_duration: float,
    source_meta: dict,
    channel_config: ChannelConfig,
) -> None:
    """Emit ``manifest.json`` — debug snapshot + caption fields for posters."""
    source_domain = source_meta.get("source_domain") or ""
    source_url = source_meta.get("source_url") or ""
    fb_caption_short, fb_first_comment, tiktok_caption = await _build_captions(
        scene_plan, source_meta, channel_config,
    )
    # Briefa v0.2 — full editable caption (long-form, copy-friendly).
    from briefa.planner.full_caption import build_full_caption
    try:
        full_caption = await build_full_caption(scene_plan, source_meta)
    except Exception as exc:
        logger.warning("full_caption build failed (%s) — using fallback", exc)
        from briefa.planner.full_caption import fallback_full_caption
        full_caption = fallback_full_caption(scene_plan, source_meta)

    # Briefa Phase 5 fix: surface citation + aspect + N-mix source list so the
    # CLI's caption "Sources cited" block + downstream debuggers can read them.
    sources_dump = []
    for s in (source_meta.get("sources") or []):
        cit = s.get("citation") or {}
        sources_dump.append({
            "source_id": s.get("source_id"),
            "kind": s.get("kind"),
            "ok": s.get("ok"),
            "error": s.get("error", ""),
            "citation": {
                "name": cit.get("name", ""),
                "domain": cit.get("domain", ""),
                "url": cit.get("url", ""),
            },
        })

    manifest = {
        "title": scene_plan.title,
        "content_type": scene_plan.content_type.value,
        "voice_rate": scene_plan.voice_rate,
        "voice_name": scene_plan.voice_name,
        "channel": scene_plan.channel,
        "aspect_ratio": getattr(scene_plan, "aspect_ratio", "9:16"),
        "length": getattr(scene_plan, "length", "short"),
        "total_duration": total_duration,
        "source_url": source_url or None,
        "source_domain": source_domain or None,
        "source_name_friendly": friendly_source_name(source_domain) if source_domain else None,
        "source_image": source_meta.get("source_image"),
        "github_stats": source_meta.get("github_stats"),
        "fb_caption_short": fb_caption_short,
        "fb_first_comment": fb_first_comment,
        "tiktok_caption": tiktok_caption,
        "full_caption": full_caption,
        "source_meta": {
            "source_kind":     source_meta.get("source_kind"),
            "primary_source_id": source_meta.get("primary_source_id"),
            "num_sources_ok":   source_meta.get("num_sources_ok"),
            "num_sources_failed": source_meta.get("num_sources_failed"),
            "aspect_hint":      source_meta.get("aspect_hint"),
            "sources":          sources_dump,
        },
        "scenes": [
            {
                "scene_index": s.scene_index,
                "layout_id": s.layout_id.value,
                "start": getattr(s, "start", None),
                "duration": getattr(s, "duration", None),
                "audio_duration": getattr(s, "audio_duration", None),
                "voice_script": s.voice_script,
                "citation_source_index": getattr(s, "citation_source_index", 0),
                "citation_name":   getattr(s, "citation_name", ""),
                "citation_domain": getattr(s, "citation_domain", ""),
                "citation_url":    getattr(s, "citation_url", ""),
            }
            for s in scene_plan.scenes
        ],
    }
    (job_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )


__all__ = [
    "compose",
    "split_voice_into_segments",
    "compute_bullet_timings",
    "LAYOUT_FILENAMES",
]
