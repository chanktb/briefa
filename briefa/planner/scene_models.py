"""Pydantic v2 schemas for the scene plan — layouts, slots, and runtime fields.

A ``Scene`` is one rendered card in a video; a ``ScenePlan`` is the full output
of the planner that the renderer consumes. Each layout has its own slot model
listing the fields the Jinja2 template expects.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .content_types import (
    DEFAULT_VOICE_NAME,
    VOICE_RATE_MAP,
    ContentType,
)

# ────────────────────── LAYOUT IDs ──────────────────────

class LayoutId(StrEnum):
    # Core 5
    TITLE_HERO = "TitleHero"
    BULLET_LIST = "BulletList"
    KPI_GRID = "KPIGrid"
    TIMELINE = "Timeline"
    CTA_OUTRO = "CTAOutro"
    # Tech / product launch (4 extra layouts)
    HERO_CARD_WITH_LOGO = "HeroCardWithLogo"   # Big squircle logo + name
    BIG_STAT_CARD = "BigStatCard"              # Half-page: LEFT name+delta · RIGHT huge number
    TERMINAL_WINDOW = "TerminalWindow"         # Mac-style terminal for install/usage
    # Screenshot embed (5 variants: browser, highlight, iphone, minimal, stack)
    SCREENSHOT_EMBED = "ScreenshotEmbed"


# ────────────────────── LAYOUT DURATIONS ──────────────────────

# Per-layout duration envelope in seconds. The renderer clamps each scene to
# ``max(audio + buffer, min)`` then ``min(., max)``. Tuned for Vietnamese
# voice pacing.
LAYOUT_DURATIONS: dict[LayoutId, dict[str, float]] = {
    LayoutId.TITLE_HERO:           {"min": 4.0, "typical": 6.0,  "max": 15.0},
    LayoutId.BULLET_LIST:          {"min": 6.0, "typical": 10.0, "max": 22.0},
    LayoutId.KPI_GRID:             {"min": 7.0, "typical": 11.0, "max": 22.0},
    LayoutId.TIMELINE:             {"min": 8.0, "typical": 13.0, "max": 25.0},
    LayoutId.CTA_OUTRO:            {"min": 4.0, "typical": 6.5,  "max": 12.0},
    LayoutId.HERO_CARD_WITH_LOGO:  {"min": 5.0, "typical": 8.0,  "max": 15.0},
    LayoutId.BIG_STAT_CARD:        {"min": 6.0, "typical": 9.0,  "max": 16.0},
    LayoutId.TERMINAL_WINDOW:      {"min": 6.0, "typical": 9.0,  "max": 18.0},
    LayoutId.SCREENSHOT_EMBED:     {"min": 5.0, "typical": 9.0,  "max": 16.0},
}


# ────────────────────── SLOT MODELS (one per layout) ──────────────────────

class TitleHeroSlots(BaseModel):
    """Big hook / opener. 1 emoji + 1-2 line title + subtitle + 1-2 tag chips."""
    model_config = ConfigDict(extra="forbid")
    icon: str = Field(..., description="Single emoji or short text glyph")
    title_top: str = Field(..., max_length=40, description="Line 1 of headline")
    title_bottom: str = Field("", max_length=40, description="Line 2 (optional)")
    highlight_word: str = Field("", max_length=20,
                                description="Word inside title to apply .highlight-hot")
    subtitle: str = Field(..., max_length=80)
    tag_chips: list[str] = Field(default_factory=list, max_length=3)


class BulletListSlots(BaseModel):
    """1-4 short points. Each is rendered with a ``#N`` marker.

    The planner picks the count:
      * 3-4 bullets in ONE scene for topics that read as a balanced list
        (the karaoke-active highlight rotates through them).
      * 1 bullet per scene when there are > 4 distinct points worth
        spotlighting — each scene then becomes a single-bullet focal
        beat, and the highlight effect holds on that one bullet the
        whole scene.
    """
    model_config = ConfigDict(extra="forbid")
    section_title: str = Field(..., max_length=40)
    highlight_word: str = Field("", max_length=20)
    bullets: list[str] = Field(..., min_length=1, max_length=4)
    # Deprecated 2026-06-29 (CEO switched to ``#N`` markers). Field kept
    # for back-compat with cached planner outputs / older render jobs —
    # template ignores it. Safe to remove in a later cleanup.
    bullet_icons: list[str] = Field(default_factory=list)


class KPIItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(..., max_length=12,
                       description="Big text or emoji, e.g. 'B1+' or '💰'")
    unit: str = Field("", max_length=8,
                     description="Optional small unit, e.g. '%' or 'm'")
    label: str = Field(..., max_length=20, description="ALL CAPS label")
    sub: str = Field("", max_length=40, description="Optional sub-caption")


class KPIGridSlots(BaseModel):
    """2x2 grid of metric cards. Use qualitative values if no numbers."""
    model_config = ConfigDict(extra="forbid")
    section_title: str = Field(..., max_length=40)
    highlight_word: str = Field("", max_length=20)
    items: list[KPIItem] = Field(..., min_length=2, max_length=4)


class TimelineStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(..., max_length=12, description="Short header, e.g. 'BƯỚC 1'")
    text: str = Field(..., max_length=50, description="Main step description")


class TimelineSlots(BaseModel):
    """Vertical numbered timeline. 3-5 steps."""
    model_config = ConfigDict(extra="forbid")
    section_title: str = Field(..., max_length=40)
    highlight_word: str = Field("", max_length=20)
    steps: list[TimelineStep] = Field(..., min_length=3, max_length=5)


class CTAOutroSlots(BaseModel):
    """Outro: icon + tagline + brand row."""
    model_config = ConfigDict(extra="forbid")
    icon: str = Field(..., max_length=4)
    title_top: str = Field(..., max_length=30)
    highlight_word: str = Field("", max_length=20)
    text: str = Field(..., max_length=60)


class HeroCardWithLogoSlots(BaseModel):
    """Big squircle logo + product name. Use when source has a fetchable logo URL.

    The renderer fills ``logo_url`` from ``source_meta.owner_avatar_url`` if
    the planner leaves it empty.
    """
    model_config = ConfigDict(extra="forbid")
    logo_url: str = Field("", max_length=300,
        description="Direct image URL (PNG/JPG). Empty = renderer fills from source_meta.")
    badge_label: str = Field("", max_length=24,
        description="Small badge below logo, e.g. 'GITHUB TRENDING'")
    title: str = Field(..., max_length=40)
    highlight_word: str = Field("", max_length=20)
    pill_text: str = Field("", max_length=80, description="Pill subtitle")
    tag_chips: list[str] = Field(default_factory=list, max_length=3)


class BigStatCardSlots(BaseModel):
    """Half-page glassmorphic card: LEFT (owner + name + delta) · RIGHT (huge number)."""
    model_config = ConfigDict(extra="forbid")
    name_top: str = Field(..., max_length=24, description="Owner prefix, e.g. 'microsoft /'")
    name_main: str = Field(..., max_length=24, description="Main name, e.g. 'markitdown'")
    delta_text: str = Field("", max_length=30,
        description="Optional delta below name, e.g. 'Trending tuần này'")
    big_value: str = Field(..., max_length=10, description="Huge number, e.g. '130K'")
    big_unit: str = Field(..., max_length=10, description="Unit, e.g. 'stars'")
    chips_grid: list[str] = Field(default_factory=list, max_length=8,
        description="Optional 2-4 row pill grid for input/output formats")


class TerminalWindowSlots(BaseModel):
    """Mac-style terminal window with command lines. For install/usage scenes."""
    model_config = ConfigDict(extra="forbid")
    badge_label: str = Field("", max_length=24,
        description="Top badge, e.g. 'Cài đặt'")
    command_lines: list[str] = Field(..., min_length=1, max_length=4,
        description="Command lines, each ≤60 chars. Include $ prefix where appropriate.")
    chips_grid: list[str] = Field(default_factory=list, max_length=4,
        description="Optional 2x2 grid of format/tech chips below terminal")


class ScreenshotEmbedSlots(BaseModel):
    """Wrap source content inside a device-chrome mockup (browser / iPhone / minimal / stack)."""
    model_config = ConfigDict(extra="forbid")
    variant: Literal["browser", "highlight", "iphone", "minimal", "stack", "mac_reader"]
    section_title: str = Field(..., max_length=50,
        description="Scene title shown above the device mockup")
    highlight_word: str = Field("", max_length=20)
    display_url: str = Field("", max_length=80,
        description="URL bar text (browser/highlight/iphone variants)")
    caption: str = Field("", max_length=140,
        description="Caption bar under window — browser variant")
    # highlight variant only
    stat_big: str = Field("", max_length=12,
        description="HIGHLIGHT: huge number for stat callout, e.g. '55%'")
    stat_text: str = Field("", max_length=100, description="HIGHLIGHT: stat sentence")
    # minimal variant only
    filename: str = Field("", max_length=40,
        description="MINIMAL: fake filename in titlebar, e.g. 'IMG_2026.jpg'")
    # stack variant only
    total_photo_count: int = Field(1, ge=1, le=20,
        description="STACK: total photo badge count")
    # iphone variant only
    article_category: str = Field("", max_length=24)
    article_headline: str = Field("", max_length=90)
    article_byline: str = Field("", max_length=50)
    article_pullquote: str = Field("", max_length=200)
    article_body: str = Field("", max_length=1200,
        description="IPHONE: body paragraphs (synth fallback when no images). "
                    "HTML allowed (<p>, <strong>).")


# ────────────────────── LAYOUT → SLOTS REGISTRY ──────────────────────

LAYOUT_SLOTS_MODEL: dict[LayoutId, type[BaseModel]] = {
    LayoutId.TITLE_HERO:           TitleHeroSlots,
    LayoutId.BULLET_LIST:          BulletListSlots,
    LayoutId.KPI_GRID:             KPIGridSlots,
    LayoutId.TIMELINE:             TimelineSlots,
    LayoutId.CTA_OUTRO:            CTAOutroSlots,
    LayoutId.HERO_CARD_WITH_LOGO:  HeroCardWithLogoSlots,
    LayoutId.BIG_STAT_CARD:        BigStatCardSlots,
    LayoutId.TERMINAL_WINDOW:      TerminalWindowSlots,
    LayoutId.SCREENSHOT_EMBED:     ScreenshotEmbedSlots,
}

SlotsUnion = Annotated[
    TitleHeroSlots | BulletListSlots | KPIGridSlots | TimelineSlots | CTAOutroSlots | HeroCardWithLogoSlots | BigStatCardSlots | TerminalWindowSlots | ScreenshotEmbedSlots,
    Field(discriminator=None),
]


# ────────────────────── SCENE & PLAN ──────────────────────

class Scene(BaseModel):
    """One scene = one layout + filled slots + voice script.

    Planner-set fields (validated): scene_index, layout_id, slots, voice_script,
    citation_source_index. Renderer-set runtime fields (default 0): start,
    duration, audio_duration, layout_filename, citation (resolved chip data).

    ``slots`` is a raw dict from the planner; ``_validate_slots_for_layout``
    runs it through ``LAYOUT_SLOTS_MODEL[layout_id]``. The renderer may swap
    it with a Pydantic instance after validation so templates can use
    attribute access.

    Briefa addition (Phase 2, see DECISIONS D005): ``citation_source_index``
    is a 1-based index into ``source_meta['sources']`` so the composer can
    bind the right citation chip to this scene. ``0`` means "no specific
    source" (e.g. CTAOutro brand row) — the composer hides the chip.
    """
    model_config = ConfigDict(extra="allow")
    scene_index: int = Field(..., ge=1)
    layout_id: LayoutId
    slots: dict | BaseModel
    voice_script: str = Field(
        ...,
        min_length=80,
        max_length=450,
        description="2-5 sentences (~180-380 chars target). No SSML. "
                    "BulletList: must read every bullet once.",
    )
    citation_source_index: int = Field(
        0, ge=0, le=32,
        description=(
            "1-based index into the BriefaInput list this scene cites. "
            "0 = no source (e.g. CTAOutro). Renderer resolves to CitationChip."
        ),
    )
    # Briefa v0.2: detailed mode allows longer voice scripts per scene so the
    # 12-15 scene case can still feel substantive (~300-400 chars vs the
    # default 180-380). short mode stays under the old envelope.
    voice_script_chars_target: int = Field(
        0, ge=0, le=600,
        description="Optional planner-set char target; 0 = use mode default.",
    )
    # Renderer-set runtime fields.
    start: float = 0.0
    duration: float = 0.0
    audio_duration: float = 0.0
    layout_filename: str = ""
    # Briefa runtime: composer fills these after binding citation_source_index.
    citation_name: str = ""
    citation_domain: str = ""
    citation_url: str = ""

    @model_validator(mode="after")
    def _validate_slots_for_layout(self) -> Scene:
        model = LAYOUT_SLOTS_MODEL.get(self.layout_id)
        if model is None:
            raise ValueError(f"Unknown layout_id: {self.layout_id}")
        if isinstance(self.slots, BaseModel):
            return self
        model.model_validate(self.slots)
        return self


class ScenePlan(BaseModel):
    """Output of the planner — everything the renderer needs.

    Briefa addition (Phase 2): ``aspect_ratio`` lets the planner know whether
    the output is 9:16 (default, Reels/TikTok/Shorts) or 16:9 (YouTube).
    Layout and safe-zone hints differ between the two; see prompts.SYSTEM_PROMPT.
    """
    model_config = ConfigDict(extra="forbid")
    content_type: ContentType
    voice_rate: str = Field(
        ...,
        pattern=r"^[+-]\d{1,3}%$",
        description="Edge-TTS rate, e.g. '+25%'",
    )
    voice_name: str = Field(default=DEFAULT_VOICE_NAME)
    channel: str = Field(default="example",
        description="Channel slug under channels/. Caller should pass the real slug.")
    title: str = Field(..., max_length=80,
                       description="Internal title (used for output dir name)")
    aspect_ratio: Literal["9:16", "16:9"] = Field(
        default="9:16",
        description="Output aspect. 9:16 = Reels/TikTok/Shorts; 16:9 = YouTube.",
    )
    length: Literal["short", "detailed"] = Field(
        default="short",
        description=(
            "short  = 5-8 scenes, target ~60-110s (default — Reels/TikTok). "
            "detailed = 10-15 scenes, target ~140-220s (deeper coverage, "
            "every source gets its own beat)."
        ),
    )
    scenes: list[Scene] = Field(..., min_length=5, max_length=15)

    @model_validator(mode="after")
    def _check_scene_count_for_length(self) -> ScenePlan:
        """Enforce the length contract end-to-end.

        Planner can drift from the prompt instructions; this validator gives
        ``run_planner`` a chance to flag the drift via its repair-prompt
        retry loop instead of shipping a 12-scene render labelled "short".

        CEO 2026-06-17 bug #4: the real bug is silent length downgrade —
        Gemini returns ``length: "short"`` even when SOURCE_META.length=
        "detailed", and the composer happily writes the short manifest.
        The fix lives in ``gemini_client.py`` (plan.length must match
        SOURCE_META.length, else retry). HERE we keep the count gate only.

        Earlier iteration of this validator also enforced a 2200-char
        total voice-script floor for detailed — got reverted 2026-06-17
        after a real run produced 1724 chars / ~123 s (well within the
        detailed envelope) and failed all 3 attempts. The scene-count
        floor + length-match check together already give the user a
        detailed-feeling video; we don't need to over-constrain voice copy.
        """
        n = len(self.scenes)
        if self.length == "short" and n > 9:
            raise ValueError(
                f"length=short cap is 8 scenes (got {n}). "
                "Either trim the plan or set length='detailed'."
            )
        if self.length == "detailed" and n < 9:
            raise ValueError(
                f"length=detailed needs ≥10 scenes (got {n}). "
                "Add more middle scenes or set length='short'."
            )
        return self

    @model_validator(mode="after")
    def _check_voice_rate_matches_content_type(self) -> ScenePlan:
        expected = VOICE_RATE_MAP[self.content_type]
        if self.voice_rate != expected:
            raise ValueError(
                f"voice_rate {self.voice_rate!r} does not match "
                f"content_type {self.content_type.value!r} (expected {expected!r})"
            )
        return self


__all__ = [
    "LayoutId",
    "LAYOUT_DURATIONS",
    "LAYOUT_SLOTS_MODEL",
    "TitleHeroSlots",
    "BulletListSlots",
    "KPIItem",
    "KPIGridSlots",
    "TimelineStep",
    "TimelineSlots",
    "CTAOutroSlots",
    "HeroCardWithLogoSlots",
    "BigStatCardSlots",
    "TerminalWindowSlots",
    "ScreenshotEmbedSlots",
    "SlotsUnion",
    "Scene",
    "ScenePlan",
]
