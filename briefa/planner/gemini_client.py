"""Gemini-driven 3-stage scene planner.

Pipeline:

  1. **Stage 1 — Extract** (``stage1_extract``)
     Gemini Flash Lite reads SOURCE and returns
     ``{topic, entity, key_points, search_queries}``.

  2. **Stage 2 — Enrich** (``stage2_enrich``)
     Gemini Flash with ``google_search`` tool runs the queries from stage 1
     and returns a ``KEY FACTS / CONTEXT / COMPARISON / SIGNIFICANCE`` block.
     We append that to SOURCE so the main planner sees both.

  3. **Stage 3 — Plan** (``plan_scenes``)
     Gemini with ``response_mime_type="application/json"`` and the
     ScenePlan JSON schema produces a strict :class:`ScenePlan`. On
     validation failure we feed the error back and retry up to
     :data:`MAX_ATTEMPTS` times.

The public entry point is :func:`plan_scenes` — it orchestrates all three
stages and returns a validated :class:`ScenePlan`.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import re
from typing import Any

from google import genai
from google.genai import types
from pydantic import ValidationError

from .content_types import (
    DEFAULT_VOICE_NAME,
    DEFAULT_VOICE_NAME_MALE,
    VOICE_NAME_BY_TYPE,
    VOICE_RATE_MAP,
    ContentType,
)
from .prompts import (
    ENRICH_SYSTEM_PROMPT,
    EXTRACT_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_meta_block,
    build_repair_prompt,
)
from .scene_models import LAYOUT_SLOTS_MODEL, LayoutId, ScenePlan

logger = logging.getLogger("briefa.planner.gemini_client")


# ════════════════════════════════════════════════════════════════════════
# Configuration
# ════════════════════════════════════════════════════════════════════════

DEFAULT_MODEL = "gemini-flash-lite-latest"     # main planner — small, JSON-strict
EXTRACT_MODEL = "gemini-flash-lite-latest"     # stage 1 — same as default
ENRICH_MODEL = "gemini-2.5-flash"              # stage 2 — Flash supports google_search

MAX_ATTEMPTS = 3
DEFAULT_CHANNEL_SLUG = "default"

# Domains where raw HTML fetch yields little useful text — always enrich.
LOW_CONTENT_DOMAINS: frozenset[str] = frozenset({
    "x.com", "twitter.com",
    "github.com",
    "gist.github.com",
    "linkedin.com",
    "instagram.com",
})


# ════════════════════════════════════════════════════════════════════════
# Client + helpers
# ════════════════════════════════════════════════════════════════════════

def get_client(api_key: str | None = None) -> genai.Client:
    """Return a ``genai.Client``.

    Key selection priority:
      1. Explicit ``api_key`` argument
      2. ``GEMINI_API_KEYS`` (plural, comma-separated) — random pick.
         Lets a deployment spread free-tier quota across multiple
         personal projects without hitting any single key's 1500
         req/day cap. Each ``get_client()`` call picks ONE at random,
         so a long-running pipeline naturally distributes load.
      3. ``GEMINI_API_KEY`` (single) — backward compat
      4. ``GOOGLE_API_KEY`` (single) — Google's older naming

    Raises :class:`ValueError` if no key is available so callers can
    degrade rather than crash with ``SystemExit``.
    """
    if api_key:
        return genai.Client(api_key=api_key)

    keys_csv = os.environ.get("GEMINI_API_KEYS", "").strip()
    if keys_csv:
        import random
        keys = [k.strip() for k in keys_csv.split(",") if k.strip()]
        if keys:
            return genai.Client(api_key=random.choice(keys))

    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise ValueError(
            "GEMINI_API_KEYS (plural, comma-sep) or GEMINI_API_KEY "
            "(or GOOGLE_API_KEY) not set — "
            "get a free key at https://aistudio.google.com/apikey"
        )
    return genai.Client(api_key=key)


def parse_json_loose(raw: str) -> dict:
    """Parse Gemini text into a dict, tolerating common framing quirks.

    Handles trailing whitespace, ```json fences, and multiple top-level
    JSON blocks (Gemini occasionally emits both the plan and a debug
    object). When the direct parse fails, walks the string to find the
    first balanced ``{...}`` block and parses that.
    """
    raw = (raw or "").strip()
    if not raw:
        raise json.JSONDecodeError("empty response", "", 0)
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```\s*$", "", raw)
        raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    start = raw.find("{")
    if start < 0:
        raise json.JSONDecodeError("no '{' found", raw, 0)
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(raw)):
        c = raw[i]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(raw[start : i + 1])
    raise json.JSONDecodeError("unbalanced braces", raw, len(raw))


def _smart_truncate(s: str, max_len: int) -> str:
    """Trim ``s`` to ``max_len`` characters, preferring the last word boundary.

    Adds an ellipsis to signal the cut. When the trim happens close to the
    start of the string (the limit is *very* small relative to the input),
    the word-boundary search is skipped so we don't drop more than half the
    content.
    """
    if len(s) <= max_len:
        return s
    cut = s[: max(max_len - 1, 1)]  # leave room for an ellipsis char
    if max_len >= 12:
        last_space = cut.rfind(" ")
        if last_space > max_len * 0.6:
            cut = cut[:last_space]
    return cut.rstrip(" ,;:.!?") + "…"


def _field_max_length(field_info) -> int | None:
    """Pull the ``max_length`` constraint off a Pydantic ``FieldInfo``."""
    for meta in getattr(field_info, "metadata", ()) or ():
        ml = getattr(meta, "max_length", None)
        if isinstance(ml, int):
            return ml
    return None


def _truncate_to_constraints(data: dict) -> dict:
    """Pre-trim Gemini output so a slightly overlong subtitle / title doesn't
    burn a retry attempt on a fix Pydantic could already do mechanically.

    Walks the top-level ``title``, each scene's ``voice_script``, and every
    string slot field whose Pydantic model declares ``max_length``. Nested
    list-of-string slots (bullets, chips, command_lines) and list-of-model
    slots (kpi items, timeline steps) are left for the retry path because
    truncation there often produces garbled text — the retry prompt does a
    better job.
    """
    if not isinstance(data, dict):
        return data

    title = data.get("title")
    if isinstance(title, str) and len(title) > 80:
        logger.info("truncated plan.title: %d -> 80 chars", len(title))
        data["title"] = _smart_truncate(title, 80)

    for scene in data.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        voice_script = scene.get("voice_script")
        if isinstance(voice_script, str) and len(voice_script) > 450:
            logger.info(
                "truncated scene %s voice_script: %d -> 450 chars",
                scene.get("scene_index", "?"), len(voice_script),
            )
            scene["voice_script"] = _smart_truncate(voice_script, 450)

        layout_str = scene.get("layout_id")
        slots = scene.get("slots")
        if not isinstance(slots, dict) or not isinstance(layout_str, str):
            continue
        try:
            layout_id = LayoutId(layout_str)
        except ValueError:
            continue
        slot_model = LAYOUT_SLOTS_MODEL.get(layout_id)
        if slot_model is None:
            continue
        for fname, finfo in slot_model.model_fields.items():
            value = slots.get(fname)
            if not isinstance(value, str):
                continue
            max_len = _field_max_length(finfo)
            if max_len and len(value) > max_len:
                logger.info(
                    "truncated %s.%s: %d -> %d chars",
                    layout_id.value, fname, len(value), max_len,
                )
                slots[fname] = _smart_truncate(value, max_len)
    return data


def _force_voice_rate(data: dict, default_channel: str) -> dict:
    """Snap ``voice_rate`` to the canonical value for ``content_type``.

    Gemini occasionally drifts (e.g. ``content_type=learning`` with
    ``voice_rate=+25%``). The ScenePlan validator would reject that;
    correcting it here saves a round-trip.

    Also defaults ``voice_name`` and ``channel`` when Gemini omits them.
    """
    ct = data.get("content_type")
    if isinstance(ct, str):
        with contextlib.suppress(ValueError, KeyError):
            data["voice_rate"] = VOICE_RATE_MAP[ContentType(ct.lower())]
    data.setdefault("voice_name", DEFAULT_VOICE_NAME)
    data.setdefault("channel", default_channel)
    return data


def _scene_plan_response_schema() -> dict:
    """JSON Schema for ScenePlan — passed to Gemini's ``response_schema``.

    Gemini's schema support is a subset of full JSON Schema; current SDKs
    tolerate the Pydantic-emitted dict. If a future SDK rejects it,
    callers fall back to ``response_mime_type`` only.
    """
    return ScenePlan.model_json_schema()


def needs_enrich(source_text: str, source_meta: dict) -> bool:
    """Decide whether to run the Stage-2 Google Search enrich pass.

    Any URL source gets enriched (we want stars, releases, comparison data
    external to the page itself). Free-text inputs get enriched only when
    very short — long user-supplied text already has enough material.
    """
    if source_meta.get("source_url"):
        return True
    return not source_text or len(source_text.strip()) < 400


# ════════════════════════════════════════════════════════════════════════
# Stage 1 — Extract topic + queries
# ════════════════════════════════════════════════════════════════════════

async def stage1_extract(
    source_text: str,
    source_meta: dict,
    client: genai.Client,
    *,
    model: str = EXTRACT_MODEL,
) -> dict:
    """Extract ``{topic, entity, key_points, search_queries}`` from SOURCE.

    Returns ``{}`` on any failure so the pipeline can degrade gracefully —
    the enrich stage then falls back to default search queries derived from
    the URL / domain.
    """
    url = source_meta.get("source_url", "")
    user_prompt = (
        f"SOURCE:\n```\n{source_text[:4000]}\n```\n\n"
        f"URL: {url or '(no URL)'}\n\n"
        f"Extract topic + key_points + search_queries (JSON only)."
    )
    try:
        resp = await asyncio.to_thread(
            client.models.generate_content,
            model=model,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=EXTRACT_SYSTEM_PROMPT,
                response_mime_type="application/json",
                temperature=0.3,
                max_output_tokens=1024,
            ),
        )
    except Exception as exc:
        logger.warning("stage1 extract failed (%s): %s", type(exc).__name__, exc)
        return {}

    try:
        data = json.loads((resp.text or "").strip() or "{}")
    except json.JSONDecodeError as exc:
        logger.warning("stage1 extract JSON parse failed: %s", exc)
        return {}

    if not isinstance(data, dict):
        return {}
    logger.info(
        "stage1 topic=%r entity=%r queries=%d",
        (data.get("topic", "") or "")[:60],
        data.get("entity", ""),
        len(data.get("search_queries", []) or []),
    )
    return data


# ════════════════════════════════════════════════════════════════════════
# Stage 2 — Google Search enrich
# ════════════════════════════════════════════════════════════════════════

async def stage2_enrich(
    source_text: str,
    source_meta: dict,
    client: genai.Client,
    *,
    extracted: dict | None = None,
    model: str = ENRICH_MODEL,
) -> str:
    """Append a ``KEY FACTS / CONTEXT / COMPARISON / SIGNIFICANCE`` block.

    Uses Gemini's ``google_search`` tool to surface facts that aren't in
    the raw SOURCE. Returns the original ``source_text`` unchanged on any
    failure or when Gemini reports ``NO_NEW_INFO``.
    """
    extracted = extracted or {}
    url = source_meta.get("source_url", "")
    topic = extracted.get("topic", "")
    entity = extracted.get("entity", "")
    key_points = extracted.get("key_points") or []
    search_queries = extracted.get("search_queries") or []
    source_preview = source_text[:1500] if source_text else "(empty)"

    if search_queries:
        queries_block = "\n".join(
            f'  {i+1}. "{q}"' for i, q in enumerate(search_queries[:5])
        )
    else:
        target = entity or topic
        queries_block = (
            f'  1. "{target} stars github"\n'
            f'  2. "{target} alternatives comparison"\n'
            f'  3. "{target} 2026 release"'
        )

    points_block = "\n".join(
        f"  - {p}" for p in (key_points[:5] if key_points else ["(source ngắn)"])
    )

    user_prompt = (
        f"TOPIC: {topic or '(unknown)'}\n"
        f"ENTITY: {entity or '(unknown)'}\n"
        f"URL: {url or '(none)'}\n\n"
        f"KEY POINTS từ source (đã biết, KHÔNG search lại những này):\n{points_block}\n\n"
        f"SEARCH QUERIES BẮT BUỘC chạy (ít nhất 3 trong số này):\n{queries_block}\n\n"
        f"SOURCE preview (chỉ tham khảo ngữ cảnh):\n```\n{source_preview}\n```\n\n"
        "NHIỆM VỤ:\n"
        "1. Chạy search cho các query trên\n"
        "2. Tổng hợp các fact MỚI mà KEY POINTS không có\n"
        "3. Output theo format yêu cầu (KEY FACTS / CONTEXT / COMPARISON / SIGNIFICANCE)\n"
    )

    try:
        resp = await asyncio.to_thread(
            client.models.generate_content,
            model=model,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=ENRICH_SYSTEM_PROMPT,
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.4,
                max_output_tokens=1500,
            ),
        )
    except Exception as exc:
        logger.warning(
            "stage2 enrich failed (%s): %s — keeping source as-is",
            type(exc).__name__, exc,
        )
        return source_text

    enriched = (resp.text or "").strip()
    if not enriched or len(enriched) < 100 or enriched.upper().startswith("NO_NEW_INFO"):
        logger.info("stage2 returned no new info (%d chars)", len(enriched))
        return source_text

    appended = (
        f"{source_text}\n\n"
        f"═══════════════════════════════════════════════════════════════\n"
        f"ADDITIONAL_RESEARCH (from Google Search — use these facts in scenes):\n"
        f"═══════════════════════════════════════════════════════════════\n"
        f"{enriched}\n"
    )
    logger.info(
        "stage2 appended %d chars of research (source %d → combined %d)",
        len(enriched), len(source_text), len(appended),
    )
    return appended


# ════════════════════════════════════════════════════════════════════════
# Stage 3 — Main planner
# ════════════════════════════════════════════════════════════════════════

async def plan_scenes(
    source_text: str,
    *,
    channel_config: dict | None = None,
    source_meta: dict | None = None,
    model: str | None = None,
    client: genai.Client | None = None,
    enable_enrich: bool | None = None,
) -> ScenePlan:
    """Turn a SOURCE block into a validated :class:`ScenePlan`.

    Args:
        source_text:     Normalized source (text, markdown, or extracted URL body).
        channel_config:  Optional dict with ``voice_name`` / ``channel``
                         overrides. Use :class:`core.channel.ChannelConfig`
                         and ``.model_dump()`` to feed it.
        source_meta:     Metadata from :mod:`core.sources.url_extract`
                         (``source_url``, ``source_domain``, ``source_image``,
                         ``github_stats``, etc.).
        model:           Override planner model id. Defaults to
                         ``$GEMINI_MODEL`` or :data:`DEFAULT_MODEL`.
        client:          Inject a pre-built ``genai.Client`` (useful in tests).
        enable_enrich:   Force the Stage-2 Google Search pass on or off.
                         ``None`` = decide based on source / meta.

    Returns:
        Validated :class:`ScenePlan`.

    Raises:
        ValueError:       no API key on the environment / parameters.
        json.JSONDecodeError, ValidationError: final failure after retries.
    """
    client = client or get_client()
    model = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    channel_config = channel_config or {}
    source_meta = source_meta or {}
    default_channel = (channel_config.get("channel") or DEFAULT_CHANNEL_SLUG)

    # Stages 1 + 2 if applicable.
    should_enrich = enable_enrich if enable_enrich is not None else needs_enrich(source_text, source_meta)
    if should_enrich:
        logger.info(
            "running 3-stage pipeline: source_len=%d, domain=%s",
            len(source_text), source_meta.get("source_domain"),
        )
        extracted = await stage1_extract(source_text, source_meta, client)
        source_text = await stage2_enrich(source_text, source_meta, client, extracted=extracted)

    # Stage 3 with retry loop.
    response_schema = _scene_plan_response_schema()
    prompt = build_meta_block(source_meta) + source_text
    last_err: Exception | None = None
    last_raw = ""

    for attempt in range(1, MAX_ATTEMPTS + 1):
        temperature = 0.7 if attempt == 1 else 0.4
        resp = await _call_gemini_with_schema_retry(
            client=client,
            model=model,
            prompt=prompt,
            response_schema=response_schema,
            temperature=temperature,
        )
        last_raw = resp.text or ""

        try:
            data = parse_json_loose(last_raw)
        except json.JSONDecodeError as exc:
            last_err = exc
            logger.warning(
                "attempt %d: JSON parse failed: %s — raw[:200]=%r",
                attempt, exc, last_raw[:200],
            )
            prompt = build_repair_prompt(source_text, last_raw, exc)
            continue

        data = _truncate_to_constraints(data)
        data = _force_voice_rate(data, default_channel)
        _apply_voice_gender_policy(data)
        if channel_config.get("voice_name"):
            data["voice_name"] = channel_config["voice_name"]
        if "channel" in channel_config:
            data["channel"] = channel_config["channel"]

        try:
            plan = ScenePlan.model_validate(data)
        except ValidationError as exc:
            last_err = exc
            logger.warning("attempt %d: ScenePlan validation failed: %s", attempt, exc)
            prompt = build_repair_prompt(source_text, last_raw, exc)
            continue

        # CEO 2026-06-17 bug #4: Gemini sometimes downgrades length to "short"
        # in its JSON output even when SOURCE_META.length="detailed" — the
        # composer then writes manifest.length=short and the user gets a 60-90s
        # video despite picking "Chi tiết đầy đủ". Reject + retry when the
        # planner's reply length disagrees with what the pipeline requested.
        requested_length = source_meta.get("length")
        if requested_length in {"short", "detailed"} and plan.length != requested_length:
            mismatch = ValueError(
                f"plan.length={plan.length!r} but SOURCE_META.length="
                f"{requested_length!r}; the two MUST match. Re-emit the plan "
                f"with length={requested_length!r} and the matching scene "
                f"count / voice_script envelope."
            )
            last_err = mismatch
            logger.warning("attempt %d: planner length drift: %s", attempt, mismatch)
            prompt = build_repair_prompt(source_text, last_raw, mismatch)
            continue

        if attempt > 1:
            logger.info("planner succeeded on attempt %d/%d", attempt, MAX_ATTEMPTS)
        return plan

    assert last_err is not None
    logger.error("planner gave up after %d attempts: %s", MAX_ATTEMPTS, last_err)
    raise last_err


async def _call_gemini_with_schema_retry(
    *,
    client: genai.Client,
    model: str,
    prompt: str,
    response_schema: dict,
    temperature: float,
) -> Any:
    """Call Gemini with a JSON ``response_schema`` and fall back to
    ``response_mime_type`` only if the schema is rejected."""
    try:
        return await asyncio.to_thread(
            client.models.generate_content,
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=response_schema,
                temperature=temperature,
                max_output_tokens=4096,
            ),
        )
    except Exception as exc:
        logger.warning(
            "Gemini API error with response_schema (%s): %s — retrying without schema",
            type(exc).__name__, exc,
        )
        return await asyncio.to_thread(
            client.models.generate_content,
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                temperature=temperature,
                max_output_tokens=4096,
            ),
        )


def _apply_voice_gender_policy(data: dict) -> None:
    """Apply the voice-gender rules in place on the planner output dict.

      - TECH / LEARNING content → male voice (matches marketing tone).
      - NEWS / STORY → random pick between male and female per video.
    """
    ct = data.get("content_type")
    if not isinstance(ct, str):
        return
    try:
        content_type_enum = ContentType(ct.lower())
    except ValueError:
        return
    if content_type_enum in (ContentType.NEWS, ContentType.STORY):
        data["voice_name"] = random.choice([DEFAULT_VOICE_NAME, DEFAULT_VOICE_NAME_MALE])
        logger.info(
            "%s content → random voice: %s",
            content_type_enum.value, data["voice_name"],
        )
    else:
        data["voice_name"] = VOICE_NAME_BY_TYPE[content_type_enum]


__all__ = [
    "DEFAULT_MODEL",
    "EXTRACT_MODEL",
    "ENRICH_MODEL",
    "MAX_ATTEMPTS",
    "LOW_CONTENT_DOMAINS",
    "get_client",
    "parse_json_loose",
    "needs_enrich",
    "stage1_extract",
    "stage2_enrich",
    "plan_scenes",
]
