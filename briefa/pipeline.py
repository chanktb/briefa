"""KTB AI News orchestrator — single input → MP4.

Two entry points:

  - :func:`run_pipeline_from_text` — text, URL, or markdown input
  - :func:`run_pipeline_from_images` — one or more screenshots (Gemini Vision
    extracts a text source, then the text pipeline runs)

Both return a :class:`PipelineResult` carrying the MP4 path, manifest dict,
and ``job_dir`` so the caller can publish or archive the artefacts.

Vendored from ktb-studio/tools/ainews/pipeline.py — imports rewired to the
in-tree ``briefa`` package.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from briefa.channel import ChannelConfig, load_channel_config
from briefa.planner import plan_scenes
from briefa.renderer import RenderResult, compose, render
from briefa.renderer.hyperframes import HYPERFRAMES_VERSION_DEFAULT
from briefa.sources import detect_and_normalize, extract_from_images
from briefa.sources.router import route_and_merge
from briefa.sources.url_extract import extract_domain, extract_url_from_text

logger = logging.getLogger("briefa.pipeline")


@dataclass
class PipelineResult:
    """End-to-end output of a single AI-News run."""
    mp4_path: Path
    manifest: dict
    job_dir: Path
    render_result: RenderResult

    @property
    def title(self) -> str:
        return self.manifest.get("title", "Untitled")

    @property
    def total_duration(self) -> float:
        return float(self.manifest.get("total_duration", 0.0))


# ════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════

def _slugify(text: str, maxlen: int = 40) -> str:
    """Lowercase ASCII slug suitable for a folder name."""
    s = unicodedata.normalize("NFKD", text)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_").lower()
    return s[:maxlen] or "untitled"


def _make_job_dir(jobs_root: Path, title: str) -> Path:
    """Create a fresh ``<jobs_root>/<UTC-timestamp>_<slug>/`` directory."""
    jobs_root = Path(jobs_root)
    jobs_root.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    job = jobs_root / f"{ts}_{_slugify(title)}"
    job.mkdir(parents=True, exist_ok=True)
    return job


def _channel_config_to_planner_dict(cfg: ChannelConfig, slug: str) -> dict:
    """Reduce ``ChannelConfig`` to the keys the planner reads."""
    return {"voice_name": cfg.voice_name, "channel": slug}


# ════════════════════════════════════════════════════════════════════════
# Pipeline (text / URL / markdown)
# ════════════════════════════════════════════════════════════════════════

async def run_pipeline_from_text(
    raw_input: str,
    channel_slug: str,
    *,
    channels_root: Path | str = "channels",
    jobs_root: Path | str = "jobs",
    hyperframes_version: str = HYPERFRAMES_VERSION_DEFAULT,
    render_timeout: float = 600.0,
    theme_variant_override: str | None = None,
) -> PipelineResult:
    """Run the full pipeline for a text / URL / markdown input.

    Steps: ``detect_and_normalize`` → ``plan_scenes`` → ``compose`` →
    ``hyperframes render``. The composer is passed ``original_text`` in
    ``source_meta`` so its iPhone-reader fallback can fire when the input
    has no images.
    """
    channels_root = Path(channels_root)
    jobs_root = Path(jobs_root)
    logger.info(
        "pipeline starting channel=%s, input %d chars",
        channel_slug, len(raw_input),
    )

    channel_cfg = load_channel_config(channel_slug, channels_root=channels_root)
    if theme_variant_override:
        channel_cfg = channel_cfg.model_copy(update={"theme_variant": theme_variant_override})
        logger.info("theme variant override: %s", theme_variant_override)

    kind, normalized, source_meta = await detect_and_normalize(raw_input)
    source_meta["original_text"] = normalized
    source_meta["source_kind"] = kind
    logger.info(
        "source kind=%s, normalized=%d chars, meta_keys=%s",
        kind, len(normalized), sorted(source_meta.keys()),
    )

    if kind == "text" and not source_meta.get("source_url"):
        embedded = extract_url_from_text(raw_input)
        if embedded:
            source_meta["source_url"] = embedded
            source_meta["source_domain"] = extract_domain(embedded)
            logger.info("embedded URL detected, source_domain=%s", source_meta["source_domain"])

    return await _run_planner_to_render(
        normalized,
        source_meta=source_meta,
        channel_cfg=channel_cfg,
        channel_slug=channel_slug,
        channels_root=channels_root,
        jobs_root=jobs_root,
        hyperframes_version=hyperframes_version,
        render_timeout=render_timeout,
    )


# ════════════════════════════════════════════════════════════════════════
# Pipeline (images via Gemini Vision)
# ════════════════════════════════════════════════════════════════════════

async def run_pipeline_from_images(
    image_paths: Sequence[Path | str],
    channel_slug: str,
    *,
    hint: str = "",
    channels_root: Path | str = "channels",
    jobs_root: Path | str = "jobs",
    hyperframes_version: str = HYPERFRAMES_VERSION_DEFAULT,
    render_timeout: float = 600.0,
    min_extracted_chars: int = 40,
    theme_variant_override: str | None = None,
) -> PipelineResult:
    """Run the full pipeline starting from one or more screenshots.

    Vision extracts a Vietnamese plain-text source first; if extraction is
    too short the ``hint`` text is used as the source so the user still gets
    a video. ``hint`` longer than ~200 chars is appended to the extracted
    text as a ``USER_CONTEXT`` block so the planner gets the full picture.

    Raises:
        ValueError: when vision yields nothing usable and no fallback hint
                    is available.
    """
    image_paths = [Path(p) for p in image_paths]
    channels_root = Path(channels_root)
    jobs_root = Path(jobs_root)
    logger.info(
        "vision pipeline starting channel=%s, %d images, hint=%d chars",
        channel_slug, len(image_paths), len(hint),
    )

    channel_cfg = load_channel_config(channel_slug, channels_root=channels_root)
    if theme_variant_override:
        channel_cfg = channel_cfg.model_copy(update={"theme_variant": theme_variant_override})
        logger.info("theme variant override: %s", theme_variant_override)

    extracted = await extract_from_images(image_paths, hint=hint)
    logger.info("vision returned %d chars", len(extracted))

    if not extracted or len(extracted) < min_extracted_chars:
        if len(hint) >= min_extracted_chars:
            logger.info("vision short, using hint as source (%d chars)", len(hint))
            extracted = hint
        else:
            raise ValueError(
                f"Vision extracted only {len(extracted)} chars and hint is too short"
            )

    if len(hint) > 200 and hint not in extracted:
        extracted = (
            f"{extracted}\n\n"
            "═══════════════════════════════════════════════════════════════\n"
            "USER_CONTEXT (caption thêm ngoài ảnh):\n"
            "═══════════════════════════════════════════════════════════════\n"
            f"{hint}"
        )
        logger.info("merged hint as USER_CONTEXT block (%d chars)", len(hint))

    source_meta: dict = {
        "source_kind": "image",
        "num_images": len(image_paths),
        "image_hint": hint,
        "photo_paths": [str(p) for p in image_paths],
        "original_text": extracted,
    }
    embedded = extract_url_from_text(hint)
    if embedded:
        source_meta["source_url"] = embedded
        source_meta["source_domain"] = extract_domain(embedded)
        logger.info("image-hint URL detected, source_domain=%s", source_meta["source_domain"])

    return await _run_planner_to_render(
        extracted,
        source_meta=source_meta,
        channel_cfg=channel_cfg,
        channel_slug=channel_slug,
        channels_root=channels_root,
        jobs_root=jobs_root,
        hyperframes_version=hyperframes_version,
        render_timeout=render_timeout,
    )


# ════════════════════════════════════════════════════════════════════════
# Shared tail (planner → compose → render)
# ════════════════════════════════════════════════════════════════════════

async def _run_planner_to_render(
    source_text: str,
    *,
    source_meta: dict,
    channel_cfg: ChannelConfig,
    channel_slug: str,
    channels_root: Path,
    jobs_root: Path,
    hyperframes_version: str,
    render_timeout: float,
) -> PipelineResult:
    """Run planner → compose → render given an already-normalized source."""
    plan = await plan_scenes(
        source_text,
        channel_config=_channel_config_to_planner_dict(channel_cfg, channel_slug),
        source_meta=source_meta,
    )
    logger.info(
        "plan: type=%s rate=%s scenes=%d title=%r",
        plan.content_type.value, plan.voice_rate, len(plan.scenes), plan.title,
    )

    job_dir = _make_job_dir(jobs_root, plan.title)
    await compose(
        plan,
        channel_cfg,
        job_dir,
        channels_root=channels_root,
        hyperframes_version=hyperframes_version,
        source_meta=source_meta,
    )
    logger.info("composer wrote project to %s", job_dir)

    result = await render(
        job_dir,
        hyperframes_version=hyperframes_version,
        timeout=render_timeout,
    )
    if not result.ok:
        raise RuntimeError(
            f"hyperframes render failed in {job_dir} "
            f"(returncode={result.returncode}): {result.stderr[-300:]}"
        )

    manifest = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
    logger.info(
        "pipeline OK: %s (%.1f MB, %.1fs)",
        result.mp4_path,
        result.mp4_path.stat().st_size / 1_048_576,
        manifest.get("total_duration", 0.0),
    )
    return PipelineResult(
        mp4_path=result.mp4_path,
        manifest=manifest,
        job_dir=job_dir,
        render_result=result,
    )


# ════════════════════════════════════════════════════════════════════════
# Pipeline (Briefa N-mix-input — Phase 4)
# ════════════════════════════════════════════════════════════════════════

async def run_pipeline_briefa(
    inputs: list[str | Path],
    channel_slug: str,
    *,
    image_hint: str = "",
    aspect_ratio: str = "9:16",
    length: str = "short",
    channels_root: Path | str = "channels",
    jobs_root: Path | str = "jobs",
    hyperframes_version: str = HYPERFRAMES_VERSION_DEFAULT,
    render_timeout: float = 600.0,
    theme_variant_override: str | None = None,
    source_overrides: list[str | None] | None = None,
) -> PipelineResult:
    """Briefa N-mix-input entry point.

    ``inputs`` is a flat list mixing any combination of:

      * URLs (article or public GitHub repo)
      * plain text / markdown strings
      * local image paths (``pathlib.Path`` or string path)

    The router fans out every input concurrently, merges them into a
    Briefa multi-source dossier with ``[SOURCE n]`` markers, then hands the
    blob to the same planner→compose→render tail as the legacy single-input
    flows. The composer's citation-chip binder picks up
    ``source_meta['sources']`` so each scene gets the right chip overlay.

    Args:
        inputs:                Mixed list, 1-32 items. Order is preserved so
                               ``citation_source_index`` aligns with the order
                               the user typed them.
        channel_slug:          Channel under ``channels/<slug>/``.
        image_hint:            Caption text passed to vision OCR (only used for
                               image inputs).
        aspect_ratio:          ``"9:16"`` (default) or ``"16:9"``. Surfaced to
                               the planner as ``source_meta['aspect_hint']``;
                               also pinned onto the returned ScenePlan.
        channels_root:         Root for ``channels/<slug>/``.
        jobs_root:             HyperFrames intermediate output root.
        hyperframes_version:   Pinned HyperFrames CLI version.
        render_timeout:        Seconds before ffmpeg/HyperFrames render kill.
        theme_variant_override: Per-render theme override.

    Returns:
        PipelineResult with the MP4 path, parsed manifest, and job dir.

    Raises:
        ValueError: when ``inputs`` is empty or every source failed to ingest.
    """
    if not inputs:
        raise ValueError("run_pipeline_briefa() needs at least one input")
    if aspect_ratio not in {"9:16", "16:9"}:
        raise ValueError(f"aspect_ratio must be '9:16' or '16:9', got {aspect_ratio!r}")
    if length not in {"short", "detailed"}:
        raise ValueError(f"length must be 'short' or 'detailed', got {length!r}")

    channels_root = Path(channels_root)
    jobs_root = Path(jobs_root)
    logger.info(
        "briefa pipeline starting channel=%s, %d input(s), aspect=%s, length=%s",
        channel_slug, len(inputs), aspect_ratio, length,
    )

    channel_cfg = load_channel_config(channel_slug, channels_root=channels_root)
    if theme_variant_override:
        channel_cfg = channel_cfg.model_copy(update={"theme_variant": theme_variant_override})
        logger.info("theme variant override: %s", theme_variant_override)

    dossier, meta, results = await route_and_merge(
        inputs,
        image_hint=image_hint,
        source_overrides=source_overrides,
    )
    ok_count = sum(1 for r in results if r.ok)
    if ok_count == 0:
        failed = [f"{r.kind}:{r.raw} ({r.error})" for r in results]
        raise ValueError(
            f"Every input failed to ingest: {failed}. "
            "Check URLs / network / GEMINI_API_KEYS."
        )
    logger.info(
        "router merged %d/%d source(s), dossier=%d chars",
        ok_count, len(results), len(dossier),
    )

    meta["aspect_hint"] = aspect_ratio
    meta["length"] = length
    # BUG fix 2026-06-14 — use clean_text (no instruction header, no [SOURCE n]
    # markers) for original_text so the composer's iPhone reader scene + the
    # voice-intro fallback don't read "BRIEFA_MULTI_SOURCE — ..." aloud.
    # Gemini still gets the full dossier as the user-message payload.
    meta["original_text"] = meta.get("clean_text") or dossier
    return await _run_planner_to_render(
        dossier,
        source_meta=meta,
        channel_cfg=channel_cfg,
        channel_slug=channel_slug,
        channels_root=channels_root,
        jobs_root=jobs_root,
        hyperframes_version=hyperframes_version,
        render_timeout=render_timeout,
    )


__all__ = [
    "PipelineResult",
    "run_pipeline_from_text",
    "run_pipeline_from_images",
    "run_pipeline_briefa",
]
