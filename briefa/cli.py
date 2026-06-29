"""Briefa CLI — N inputs (URL article + GitHub repo + text + images) -> 1 MP4.

Usage::

    python -m briefa \\
        --input "https://vnexpress.net/some-article" \\
        --input "https://github.com/anthropics/courses" \\
        --input "Tôi muốn nhấn mạnh đoạn data 5 năm" \\
        --image ./chart.png \\
        --image ./screenshot.png \\
        --image-hint "Tin sáng nay" \\
        --aspect 9:16 \\
        --channel example-vn

Every ``--input`` flag adds one item (URL, plain text, or text). Every
``--image`` flag adds one local image (Gemini Vision OCRs it). Order is
preserved so ``citation_source_index`` aligns with the order on the command
line. CTAOutro is always the last scene; every other scene gets a per-scene
citation chip overlay when the planner cites a source.

Output structure (under ``output/<channel>/<YYYY-MM-DD_HHMM>/``):

  * ``output.mp4``      — final 1080×1920 (9:16) or 1920×1080 (16:9) MP4
  * ``caption.txt``     — FB caption + first comment + source attribution
  * ``manifest.json``   — scene timings, voice, source meta, citation map

No auto-posting. You upload the MP4 manually after reviewing it.

See ``CLAUDE_CONTEXT.md`` and ``BRIEF.md`` at the repo root for the canonical
project brief.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from briefa.pipeline import PipelineResult, run_pipeline_briefa
from briefa.renderer.hyperframes import HYPERFRAMES_VERSION_DEFAULT
from briefa.utils.audio_measure import measure_audio_duration

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CHANNELS_DIR = PROJECT_ROOT / "channels"
DEFAULT_OUT_DIR = PROJECT_ROOT / "output"
DEFAULT_JOBS_DIR = PROJECT_ROOT / "jobs"

_VALID_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_VALID_ASPECTS = ("9:16", "16:9")

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger("briefa.cli")


@dataclass
class CliRunResult:
    mp4_path: Path
    manifest: dict
    job_dir: Path


# ════════════════════════════════════════════════════════════════════════
# helpers
# ════════════════════════════════════════════════════════════════════════


def _slugify(text: str, maxlen: int = 60) -> str:
    s = unicodedata.normalize("NFKD", text)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_").lower()
    return s[:maxlen] or "untitled"


def _list_channels() -> list[str]:
    if not CHANNELS_DIR.is_dir():
        return []
    return sorted(
        p.name for p in CHANNELS_DIR.iterdir()
        if p.is_dir() and (p / "channel.env").is_file()
    )


def _validate_image_paths(paths: list[Path]) -> list[Path]:
    resolved: list[Path] = []
    for p in paths:
        p = p.expanduser().resolve()
        if not p.is_file():
            raise SystemExit(f"Image not found: {p}")
        if p.suffix.lower() not in _VALID_IMAGE_SUFFIXES:
            raise SystemExit(
                f"Unsupported image type {p.suffix} — need one of "
                f"{sorted(_VALID_IMAGE_SUFFIXES)}"
            )
        resolved.append(p)
    return resolved


def _write_caption_file(
    out_dir: Path,
    manifest: dict,
    sources_summary: list[str],
) -> None:
    title = manifest.get("title", "Untitled")
    caption_short = (manifest.get("fb_caption_short") or "").strip()
    first_comment = (manifest.get("fb_first_comment") or "").strip()
    tiktok = (manifest.get("tiktok_caption") or "").strip()

    parts = [f"# {title}", "", caption_short]
    if first_comment:
        parts += ["", "---", "First comment:", "", first_comment]
    if tiktok:
        parts += ["", "---", "TikTok caption:", "", tiktok]
    if sources_summary:
        parts += ["", "---", "Sources cited (per scene):"]
        parts += [f"  • {line}" for line in sources_summary]
    (out_dir / "caption.txt").write_text("\n".join(parts), encoding="utf-8")


def _stage_artifacts(result: PipelineResult, output_dir: Path) -> CliRunResult:
    """Copy MP4 + manifest into the user-facing output folder + write caption."""
    output_dir.mkdir(parents=True, exist_ok=True)
    mp4_dest = output_dir / "output.mp4"
    shutil.copy2(result.mp4_path, mp4_dest)
    shutil.copy2(result.job_dir / "manifest.json", output_dir / "manifest.json")

    # Build the human-readable "sources cited" list from manifest. The
    # composer writes one entry per ingested source under source_meta.sources;
    # we surface successful ones first.
    sources_summary: list[str] = []
    source_meta = result.manifest.get("source_meta") or {}
    for src in source_meta.get("sources") or []:
        if not src.get("ok"):
            continue
        cit = src.get("citation") or {}
        name = cit.get("name") or src.get("raw") or "(unknown)"
        url = cit.get("url") or ""
        sources_summary.append(f"{name}  {url}".rstrip())

    _write_caption_file(output_dir, result.manifest, sources_summary)

    size_mb = mp4_dest.stat().st_size / 1_048_576
    try:
        dur_s = measure_audio_duration(mp4_dest)
    except Exception:
        dur_s = 0.0
    log.info("Done · %s (%.1f MB, %.1fs)", mp4_dest, size_mb, dur_s)
    return CliRunResult(mp4_path=mp4_dest, manifest=result.manifest, job_dir=result.job_dir)


# ════════════════════════════════════════════════════════════════════════
# argparse
# ════════════════════════════════════════════════════════════════════════


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="briefa",
        description=(
            "Factual briefing video — không sáng tác. "
            "N inputs (URL article + GitHub repo + text + images) -> 1 MP4."
        ),
    )

    parser.add_argument(
        "--input", "-i",
        action="append",
        default=[],
        metavar="STR",
        help=(
            "Add one input — URL (article or GitHub repo), or plain text. "
            "Pass --input multiple times to mix sources. Order matters: it "
            "controls citation_source_index per scene."
        ),
    )
    parser.add_argument(
        "--image",
        action="append",
        type=Path,
        default=[],
        metavar="PATH",
        help=(
            "Add one local image (png/jpg/webp/gif/bmp). Gemini Vision OCRs "
            "every image. Pass --image multiple times for up to 5 images."
        ),
    )
    parser.add_argument(
        "--image-hint",
        default="",
        help="Optional caption / context passed to Gemini Vision for image inputs.",
    )

    parser.add_argument(
        "--channel",
        help="Channel slug under channels/<slug>/.",
    )
    parser.add_argument(
        "--aspect",
        default="9:16",
        choices=list(_VALID_ASPECTS),
        help="Output aspect ratio. 9:16 for Reels/TikTok/Shorts, 16:9 for YouTube.",
    )
    parser.add_argument(
        "--length",
        default="short",
        choices=["short", "detailed"],
        help=(
            "short = 5-8 scenes, 60-110s (default, Reels/TikTok cadence). "
            "detailed = 10-15 scenes, 140-220s (deep brief, every source gets "
            "a dedicated beat)."
        ),
    )
    parser.add_argument(
        "--theme", "--style",
        dest="theme",
        default=None,
        choices=["default", "dark", "vivid", "bright", "corporate"],
        help=(
            "Theme variant override. 'default' = use channel's THEME_VARIANT. "
            "Other values swap in that variant for this one render."
        ),
    )
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_OUT_DIR,
        help="User-facing output root (default: output/).",
    )
    parser.add_argument(
        "--jobs-root", type=Path, default=DEFAULT_JOBS_DIR,
        help="HyperFrames intermediate folder (default: jobs/).",
    )
    parser.add_argument(
        "--render-timeout", type=float, default=600.0,
        help="Hard timeout for HyperFrames render in seconds (default: 600).",
    )
    parser.add_argument(
        "--list-channels", action="store_true",
        help="List available channels and exit.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


# ════════════════════════════════════════════════════════════════════════
# main
# ════════════════════════════════════════════════════════════════════════


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    load_dotenv(PROJECT_ROOT / ".env", override=False)

    if args.list_channels:
        channels = _list_channels()
        if not channels:
            print("No channels found. Copy channels/example-vn/ to start.")
            return 0
        print("Available channels:")
        for c in channels:
            print(f"  - {c}")
        return 0

    if not args.channel:
        parser.error("--channel <slug> is required (run --list-channels to see options).")

    # Merge inputs in input order: --input first, then --image.
    inputs: list[str | Path] = list(args.input)
    if args.image:
        image_paths = _validate_image_paths(list(args.image))
        if len(image_paths) > 5:
            parser.error(f"Maximum 5 images allowed, got {len(image_paths)}.")
        inputs.extend(image_paths)

    if not inputs:
        parser.error(
            "Provide at least one input — pass --input STR and/or --image PATH "
            "(repeat the flags to mix sources)."
        )
    if len(inputs) > 32:
        parser.error(f"Maximum 32 inputs allowed, got {len(inputs)}.")

    channel_dir = CHANNELS_DIR / args.channel
    if not channel_dir.is_dir():
        raise SystemExit(
            f"Channel '{args.channel}' not found under {CHANNELS_DIR}.\n"
            f"Available: {', '.join(_list_channels()) or '(none)'}"
        )

    theme_override = None if args.theme in (None, "default") else args.theme

    slug = _slugify(
        args.channel + "_" + datetime.now().strftime("%Y-%m-%d_%H%M")
    )
    output_dir = args.out / args.channel / slug

    log.info(
        "Briefa run · channel=%s · aspect=%s · length=%s · %d input(s) · %d image(s)",
        args.channel, args.aspect, args.length, len(args.input), len(args.image),
    )

    try:
        result = asyncio.run(run_pipeline_briefa(
            inputs,
            args.channel,
            image_hint=args.image_hint or "",
            aspect_ratio=args.aspect,
            length=args.length,
            channels_root=CHANNELS_DIR,
            jobs_root=args.jobs_root,
            hyperframes_version=HYPERFRAMES_VERSION_DEFAULT,
            render_timeout=args.render_timeout,
            theme_variant_override=theme_override,
        ))
        cli_result = _stage_artifacts(result, output_dir)
    except SystemExit:
        raise
    except ValueError as exc:
        log.error("Input error: %s", exc)
        return 1
    except Exception:  # noqa: BLE001 — surface every other failure
        log.exception("Pipeline failed")
        return 2

    print()
    print("Done.")
    print(f"  Video:    {cli_result.mp4_path}")
    print(f"  Caption:  {cli_result.mp4_path.parent / 'caption.txt'}")
    print(f"  Manifest: {cli_result.mp4_path.parent / 'manifest.json'}")
    print()
    print("Open the folder, grab the files, review, and upload manually.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
