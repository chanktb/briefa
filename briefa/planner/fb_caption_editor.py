"""Edit the voice-over transcript into an FB-friendly long-form caption.

Anh's complaint with the old caption format: numbered ``1. ... 2. ...``
lists of raw voice-over read like a script transcript, not a Facebook
post. The voice-over is optimised for *listening* (1080p Reels TTS) and
keeps filler words / "thực ra" / "nói chung" / short choppy phrasing
that work for ears but not eyes.

This module makes one additional Gemini call after the scene plan is
locked in, asking the model to rewrite the per-scene voice scripts into
a single edited, scroll-friendly Facebook post:

  - A 1-line attention hook (emoji + punchy sentence)
  - 1-2 sentence context paragraph
  - Each scene becomes a short paragraph headed by a rotating icon
    (🔹 📌 ⚡ 💡 🎯) instead of "1.", "2.", "3."
  - A 1-line wrap-up at the end

The composer (:func:`core.renderer.composer._build_captions`) takes this
edited text and tacks on the channel's hashtag toggle + the source URL
hint when one is available.

Failure mode: any exception from the Gemini call falls back to
:func:`fallback_caption` which produces a similar shape using just the
raw voice scripts and the rotating-icon set — so the render pipeline
never breaks because the caption editor was unavailable.
"""
from __future__ import annotations

import logging

from google.genai import types

from .gemini_client import get_client
from .scene_models import ScenePlan

logger = logging.getLogger("briefa.planner.fb_caption_editor")

EDITOR_MODEL = "gemini-flash-lite-latest"  # text-only, fast + free-tier friendly

# Cycle these glyphs in order across paragraphs. Anh's spec — chosen for
# visual variety + recognisability on a small phone screen.
SECTION_ICONS = ["🔹", "📌", "⚡", "💡", "🎯"]


SYSTEM_PROMPT_VI = """\
Bạn là biên tập viên Facebook chuyên nghiệp, viết caption tiếng Việt
cho 1 video tin tức / kiến thức công nghệ trên fanpage. Đầu vào của
bạn: TIÊU ĐỀ video + transcript voice-over (giọng đọc) từng cảnh đã
viết sẵn cho TTS.

Nhiệm vụ: BIÊN TẬP LẠI thành 1 caption Facebook chuẩn để người đọc
scroll qua dừng lại đọc, KHÔNG dán nguyên transcript.

YÊU CẦU CỤ THỂ:

1. **HOOK** — Mở đầu bằng 1 dòng giật mắt (1 emoji + 1 câu ngắn ~10-20
   chữ). Tránh sáo rỗng kiểu "Bạn có biết...?".

2. **INTRO** — 1-2 câu set context (cái gì đang xảy ra, vì sao quan
   trọng). Tự nhiên, không lê thê.

3. **THÂN BÀI** — Mỗi điểm chính / cảnh = 1 đoạn riêng:
   - Đầu đoạn: 1 icon dẫn dắt (em sẽ tự gán icon, anh viết "•" làm
     placeholder cũng được).
   - **Tiêu đề ngắn 4-10 chữ** (bold-able, nhưng đừng dùng markdown).
   - Xuống dòng, 2-4 câu body biên tập gọn gàng, có ý chính + 1 fact
     hoặc số liệu nếu có.
   - LOẠI BỎ filler ("thực ra", "nói chung", "có thể nói rằng",
     "tóm lại là"), LOẠI BỎ cấu trúc đọc nghe-thì-được-đọc-thì-thừa.
   - Có thể gộp 2 cảnh ngắn liên quan vào 1 đoạn, hoặc tách 1 cảnh
     dài thành 2 đoạn.

4. **CHỐT** — 1 câu rút kết hoặc câu hỏi mở để người xem comment
   tương tác.

5. **KHÔNG VIẾT**:
   - KHÔNG đưa URL nguồn vào caption (em sẽ post URL ở comment).
   - KHÔNG ghi câu "URL nguồn ở comment 👇" (composer thêm tự động).
   - KHÔNG thêm hashtag (composer xử lý theo cấu hình channel).
   - KHÔNG đánh số 1. 2. 3.
   - KHÔNG dùng markdown như **bold** hay # heading.

6. **ĐỘ DÀI** — Tổng khoảng 800-1400 ký tự. Đủ thông tin, không lê thê.

7. **GIỌNG VIẾT** — Vietnamese tự nhiên, ngôi xưng anh em / mình bạn /
   đôi bên (tuỳ tone). Không cứng nhắc kiểu báo chí, không quá teen.

Trả về CHỈ phần caption đã biên tập, KHÔNG kèm giải thích, KHÔNG bọc
trong code block, KHÔNG có dấu --- phân cách.
"""


def _build_user_prompt(plan: ScenePlan) -> str:
    """Compose the user-message payload from the scene plan."""
    lines = [
        f"TIÊU ĐỀ: {plan.title}",
        "",
        "TRANSCRIPT VOICE-OVER TỪNG CẢNH:",
    ]
    for s in plan.scenes:
        lines.append(f"[Cảnh {s.scene_index}] {s.voice_script.strip()}")
    lines += [
        "",
        "→ Biên tập thành 1 caption Facebook theo các yêu cầu trên.",
    ]
    return "\n".join(lines)


def _inject_icons(text: str) -> str:
    """Replace placeholder bullets ("•" / "- " / "* ") with rotating icons.

    The model is told to use "•" as a placeholder; we cycle through
    :data:`SECTION_ICONS` so each paragraph gets a distinct glyph.
    Anything the model emits that already looks like an icon (Misc
    Symbols / Emoji block) is left alone.
    """
    out_lines: list[str] = []
    icon_idx = 0
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.lstrip()
        # Replace common placeholder bullets at the start of a paragraph
        # heading line. We don't touch lines that already begin with a
        # high-codepoint glyph (emoji).
        if stripped.startswith(("• ", "- ", "* ", "·")):
            head = stripped.lstrip("•-*· ").strip()
            icon = SECTION_ICONS[icon_idx % len(SECTION_ICONS)]
            icon_idx += 1
            line = f"{icon} {head}"
        out_lines.append(line)
    return "\n".join(out_lines).strip()


async def edit_fb_caption(plan: ScenePlan, *, timeout_s: float = 25.0) -> str:
    """Run Gemini Flash Lite to biên-tập transcript → FB caption.

    Returns the edited caption string (no URL, no hashtags — composer
    bolts those on). On any failure falls back to
    :func:`fallback_caption`.
    """
    try:
        client = get_client()
    except Exception as exc:  # noqa: BLE001
        logger.warning("fb caption editor: no Gemini client (%s) — fallback", exc)
        return fallback_caption(plan)

    user_text = _build_user_prompt(plan)
    cfg = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT_VI,
        temperature=0.7,
        max_output_tokens=2048,
        response_mime_type="text/plain",
    )

    try:
        import asyncio
        resp = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=EDITOR_MODEL,
                contents=user_text,
                config=cfg,
            ),
            timeout=timeout_s,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("fb caption editor: Gemini call failed (%s) — fallback", exc)
        return fallback_caption(plan)

    raw = (getattr(resp, "text", "") or "").strip()
    if not raw:
        logger.warning("fb caption editor: empty response — fallback")
        return fallback_caption(plan)

    edited = _inject_icons(raw)
    # Quick sanity check — if the model returned something far too short
    # (model hallucinated or refused), fall back. ~150 chars is the
    # threshold below which we'd be worse off than the simple fallback.
    if len(edited) < 150:
        logger.warning(
            "fb caption editor: response too short (%d chars) — fallback",
            len(edited),
        )
        return fallback_caption(plan)

    logger.info(
        "fb caption editor: edited %d scenes → %d-char caption",
        len(plan.scenes), len(edited),
    )
    return edited


def fallback_caption(plan: ScenePlan) -> str:
    """Build a caption from voice scripts alone — used when Gemini is down.

    Produces a serviceable post even with no LLM access:
      - 🚀 + title as the hook
      - One rotating-icon paragraph per scene with the raw voice script
        as the body (no editing, but at least properly broken up)
      - A simple closing line
    """
    lines: list[str] = [f"🚀 {plan.title}", ""]
    for i, s in enumerate(plan.scenes):
        icon = SECTION_ICONS[i % len(SECTION_ICONS)]
        text = (s.voice_script or "").strip()
        lines.append(f"{icon} {text}")
        lines.append("")
    lines.append("👇 Anh em thấy sao? Comment để mình biết nhé.")
    return "\n".join(lines).strip()


__all__ = ["edit_fb_caption", "fallback_caption", "SECTION_ICONS"]
