"""Gemini Vision OCR — extract content from user-sent screenshots.

Used when the user forwards one or more images (tweet screenshot, FB post,
LinkedIn snippet, article excerpt photo, etc.) instead of a URL or text.
Login-walled sources become accessible: screenshot → Gemini Vision →
Vietnamese plain text → planner.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from google import genai
from google.genai import types

logger = logging.getLogger("briefa.sources.vision")


VISION_SYSTEM_PROMPT = """Bạn là expert extractor cho video content pipeline.

Bạn nhận 1 hoặc nhiều ẢNH SCREENSHOT từ user (thường là social media post:
tweet X/Twitter, FB post, LinkedIn, Instagram, article excerpt, comment thread...).

NHIỆM VỤ: ĐỌC TẤT CẢ TEXT trong ảnh và viết thành 1 đoạn TIẾNG VIỆT
plain text giàu thông tin (400-1200 chars), để pipeline sau đó dùng làm SOURCE cho video.

Trích xuất đầy đủ:
1. NỘI DUNG CHÍNH (priority #1):
   - Toàn bộ text post/tweet/article visible
   - Nếu source là tiếng Anh, DỊCH sang tiếng Việt tự nhiên nhưng GIỮ NGUYÊN các
     English technical terms (GPT, LLM, GitHub, ChatGPT, MarkItDown, Claude, API…)
   - Nếu source là tiếng Việt, giữ nguyên

2. AUTHOR + PLATFORM:
   - Username/handle (vd "@ClaudeAI", "@elonmusk")
   - Display name nếu khác handle
   - Platform: X/Twitter, Facebook, LinkedIn, Instagram, Reddit, HackerNews...

3. METADATA visible:
   - Ngày/giờ post
   - Like/retweet/comment count
   - Verified badge nếu có
   - Quote-retweet hoặc thread parent nếu visible

4. CONTEXT xung quanh:
   - Thread context (reply, parent post)
   - Embedded link nếu có
   - Multiple images = thread/album → ghép thông tin

═══════════════════════════════════════════════════════════════
OUTPUT FORMAT (PLAIN TEXT, KHÔNG markdown, KHÔNG bullet)
═══════════════════════════════════════════════════════════════

Viết liền mạch như 1 đoạn báo cáo. Format gợi ý:

"[Platform] post từ [@username] ([display name nếu có]) đăng [ngày]. Nội dung:
'[QUOTE NGUYÊN VĂN HOẶC DỊCH VIỆT]'. [Metadata: X likes, Y retweets]. [Thread
context: reply tới ai, quote ai]. [Tóm tắt 1-2 câu chủ đề chính của post]."

Nếu nhiều ảnh thuộc cùng 1 thread/post → ghép thành 1 đoạn liền mạch.
Nếu user có hint (caption khi gửi ảnh), DÙNG nó làm focus topic.

QUY TẮC CỨNG:
- KHÔNG suy diễn quá nhiều — chỉ trích xuất những gì THẤY trong ảnh.
- KHÔNG fake số liệu nếu không visible trong ảnh.
- KHÔNG thêm comment cá nhân.
- Trả về MỘT đoạn duy nhất, không xuống dòng nhiều.
"""


_MIME_BY_EXT: dict[str, str] = {
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
    ".webp": "image/webp",
    ".gif":  "image/gif",
    ".bmp":  "image/bmp",
}


def _detect_mime(path: Path) -> str:
    return _MIME_BY_EXT.get(path.suffix.lower(), "image/jpeg")


def _resolve_api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise ValueError(
            "GEMINI_API_KEY (or GOOGLE_API_KEY) not set — "
            "get one at https://aistudio.google.com/apikey"
        )
    return key


# Vision-capable Gemini models, ordered from "preferred" to "lowest-quota
# fallback". The first entry has the best output quality but the smallest
# free-tier quota; later entries trade quality for headroom so the pipeline
# survives a 429 RESOURCE_EXHAUSTED on the primary model.
_VISION_MODEL_CHAIN: list[str] = [
    "gemini-2.5-flash",            # primary — best vision quality (low quota)
    "gemini-flash-latest",         # fallback — wider quota, still multimodal
    "gemini-flash-lite-latest",    # last resort — highest quota, smaller model
]


def _is_quota_error(exc: BaseException) -> bool:
    """Heuristic: Gemini SDK raises ``ClientError(429, ...)`` for quota hits."""
    msg = str(exc)
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg


async def extract_from_images(
    image_paths: list[Path | str],
    hint: str = "",
    model: str | None = None,
) -> str:
    """Use Gemini multimodal to extract Vietnamese plain-text from screenshots.

    The result is suitable to feed back into ``detect_and_normalize`` as
    free-form text — the planner pipeline will treat it like any other
    text source.

    Args:
        image_paths: One or more local image files. Missing files are skipped.
        hint: Optional user caption hint to focus the extraction.
        model: Override the primary model. When ``None`` (default), the
               function walks :data:`_VISION_MODEL_CHAIN` so a 429 on the
               primary model gracefully falls back to a higher-quota one
               instead of crashing the pipeline.

    Returns:
        Extracted Vietnamese text. Falls back to ``hint`` if every model
        in the chain fails or the response is empty.

    Raises:
        ValueError: if ``GEMINI_API_KEY`` is not set in the environment.
    """
    if not image_paths:
        return hint or ""

    api_key = _resolve_api_key()
    client = genai.Client(api_key=api_key)

    user_text = (
        f"User hint về chủ đề (có thể trống): {hint or '(không có)'}\n\n"
        f"Đây là {len(image_paths)} ảnh screenshot. Trích xuất content theo nhiệm vụ trên."
    )

    parts: list = []
    for raw in image_paths:
        path = Path(raw)
        if not path.exists():
            logger.warning("vision: image not found, skipping: %s", path)
            continue
        with path.open("rb") as f:
            data = f.read()
        parts.append(types.Part.from_bytes(data=data, mime_type=_detect_mime(path)))
    parts.append(types.Part.from_text(text=user_text))

    if len(parts) == 1:  # only the prompt text — every image was missing
        return hint or ""

    # When the caller pins a specific model, honour it; otherwise iterate the
    # fallback chain so a 429 doesn't kill the pipeline.
    chain = [model] if model else _VISION_MODEL_CHAIN
    last_err: Exception | None = None

    for candidate in chain:
        try:
            resp = await asyncio.to_thread(
                client.models.generate_content,
                model=candidate,
                contents=parts,
                config=types.GenerateContentConfig(
                    system_instruction=VISION_SYSTEM_PROMPT,
                    temperature=0.3,
                    max_output_tokens=2048,
                ),
            )
        except Exception as exc:
            last_err = exc
            if _is_quota_error(exc) and candidate is not chain[-1]:
                logger.warning(
                    "vision: %s hit quota — falling back to next model in chain",
                    candidate,
                )
                continue
            # Any other error (auth, malformed image, etc.) is final.
            logger.warning(
                "vision: %s failed (%s): %s — returning hint",
                candidate, type(exc).__name__, exc,
            )
            return hint or ""
        text = (resp.text or "").strip()
        if text:
            if candidate is not chain[0]:
                logger.info("vision: served by fallback model %s", candidate)
            return text
        # Empty response — try the next model.
        logger.info("vision: %s returned empty, trying next", candidate)

    logger.warning(
        "vision: every model in the chain failed (last=%s) — returning hint",
        last_err,
    )
    return hint or ""


__all__ = ["extract_from_images", "VISION_SYSTEM_PROMPT"]
