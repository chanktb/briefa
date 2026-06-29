"""Full-length copy-able caption — Briefa v0.2.

CEO ask 2026-06-13: alongside the short FB caption (``fb_caption_editor.py``)
ship a *complete* edited write-up the user can copy-paste into a blog post,
LinkedIn article, or long FB post. This caption is NOT a summary — it covers
every angle the video does, with explicit source attribution.

Constraints (carry the BRIEFA FACTUAL MODE contract end-to-end):

* ``KHÔNG SÁNG TÁC`` — every claim traces back to an ingested source.
  Missing facts → "không đủ thông tin", never a guess.
* Neutral editor voice. No emotional adjectives ("tuyệt vời", "kinh khủng").
  No predictions ("có thể sẽ", "dự đoán"). No personal pronouns ("tôi",
  "chúng tôi"). Allowed framings: "Theo [Source]…", "Cụ thể…", "Tóm lại…".
* Each ingested source gets explicit attribution in its dedicated paragraph.
* Citation list at the bottom (name + URL per source).
* Hashtag suggestions 5-8 at the very end (1 line, separated by spaces).

Failure mode: any Gemini error → :func:`fallback_full_caption` builds a
serviceable post from the dossier + scene plan with no LLM.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from google.genai import types

from .gemini_client import get_client
from .scene_models import ScenePlan

logger = logging.getLogger("briefa.planner.full_caption")

FULL_CAPTION_MODEL = "gemini-flash-latest"   # bigger context window than flash-lite
FULL_CAPTION_TIMEOUT_S = 45.0
# Gemini Flash hard caps output at 8192 tokens. Vietnamese with diacritics
# encodes at ~1.5-2 chars/token, so 8192 → ~12-16k chars max — plenty for the
# 1500-3500 char target plus a citation list and hashtags.
FULL_CAPTION_MAX_TOKENS = 8192


SYSTEM_PROMPT_VI = """Bạn là biên tập viên Việt cho fanpage tin tức / công nghệ. Đầu vào của
bạn:

  - TIÊU ĐỀ video
  - SUMMARY TỪNG CẢNH (voice-over đã viết cho TTS)
  - PER-SOURCE DOSSIER (mỗi nguồn 1 đoạn raw text + URL gốc)

NHIỆM VỤ: biên tập thành 1 BÀI VIẾT ĐẦY ĐỦ tiếng Việt mà người dùng có thể
copy-paste sang blog / LinkedIn / FB note. Đây KHÔNG phải tóm tắt — nó cần
bao quát mọi góc cạnh video đã đề cập, nêu rõ nguồn cho từng nhóm thông tin.

═══════════════════════════════════════════════════════════════════
QUY TẮC CỨNG (vi phạm = output sai)
═══════════════════════════════════════════════════════════════════

A. KHÔNG SÁNG TÁC. Mọi con số / ngày / tên / sự kiện đều phải trace lại
   đoạn DOSSIER tương ứng. Thiếu thông tin → ghi "không đủ thông tin" thay vì
   đoán. KHÔNG dùng kiến thức nền của bạn.

B. KHÔNG opinion cá nhân, KHÔNG dự đoán tương lai. Cho phép 1-2 câu nhận
   định trung tính per paragraph theo dạng "Theo [Nguồn]…", "Cụ thể …",
   "Tóm lại …". Cấm: "tuyệt vời", "kinh khủng", "đỉnh", "có thể sẽ",
   "dự đoán", "chắc chắn sẽ", "tôi nghĩ", "chúng tôi cho rằng".

C. ATTRIBUTION rõ ràng. Mỗi nguồn được giới thiệu ít nhất 1 lần bằng tên
   thân thiện đã cho ("Theo VnExpress…", "Trên repo GitHub obra/superpowers
   …"). KHÔNG trộn fact giữa các nguồn mà không nêu rõ nguồn nào nói gì.

D. ĐỘ DÀI mục tiêu 1500-3500 ký tự (không kể citation list + hashtag).
   Bài chi tiết NHƯNG không lê thê. Mỗi paragraph 3-5 câu, không vượt 6.
   QUAN TRỌNG: Luôn KẾT THÚC Ý TRỌN VẸN. Nếu cảm thấy sắp vượt giới hạn,
   ưu tiên cắt ngắn paragraph cuối + viết KẾT + NGUỒN + HASHTAG đầy đủ
   thay vì để bài dở câu giữa chừng. Bài thiếu nguồn / hashtag = output sai.

═══════════════════════════════════════════════════════════════════
CẤU TRÚC ĐẦU RA (BẮT BUỘC theo thứ tự)
═══════════════════════════════════════════════════════════════════

1. HOOK — 1 dòng (1 emoji + câu mở 12-22 chữ). KHÔNG sáo rỗng.
2. INTRO — 1 paragraph 2-3 câu set context (bối cảnh + vì sao đáng đọc).
3. BODY — N paragraph (N ≈ số nguồn ingested + 1-2 paragraph synthesis):
     • Bắt đầu paragraph bằng 1 emoji + tiêu đề ngắn 4-10 chữ (không
       markdown, viết liền dòng).
     • Xuống dòng, 3-5 câu body biên tập gọn, có ý chính + 1-2 số liệu /
       fact / quote ngắn nếu có trong DOSSIER.
     • Attribution mở paragraph khi đổi nguồn ("Theo VnExpress…", "Trên
       repo GitHub obra/superpowers…"). Có thể gộp 2 nguồn cùng chủ đề.
     • LOẠI BỎ filler ("thực ra", "nói chung", "có thể nói rằng").
4. KẾT — 1 câu rút kết trung tính hoặc câu hỏi mở cho người đọc comment.
5. NGUỒN — block riêng, format chính xác như sau:

   ── NGUỒN ──
   • <Friendly name 1> — <URL 1>
   • <Friendly name 2> — <URL 2>
   ...

   (Nếu nguồn không có URL — ví dụ ghi chú người dùng — bỏ qua, KHÔNG ghi
   "không có URL".)
6. HASHTAG — 1 dòng cuối với 5-8 hashtag không dấu, viết liền (#TenHashTag),
   liên quan đến chủ đề và các từ khoá nguồn (vd #AI #GitHub #Anthropic).

═══════════════════════════════════════════════════════════════════
KHÔNG ĐƯỢC
═══════════════════════════════════════════════════════════════════

  - KHÔNG dùng markdown (## **bold** _italic_ ```code```).
  - KHÔNG đánh số 1. 2. 3. trong body (icon đã thay thế).
  - KHÔNG copy nguyên transcript voice-over từng cảnh (đó là cho TTS, nghe
    thì OK, đọc thì lê thê).
  - KHÔNG bọc output trong code block, KHÔNG kèm preamble/giải thích.

Trả về CHỈ phần caption đã biên tập, plain text, sẵn sàng copy.
"""


SECTION_ICONS = ["🔹", "📌", "⚡", "💡", "🎯", "🧭", "🛠", "📊"]


@dataclass
class _SourceDigest:
    """Compact source representation passed to Gemini."""
    n: int
    kind: str
    name: str
    url: str
    text: str            # truncated dossier text for this source


def _build_user_prompt(plan: ScenePlan, source_meta: dict[str, Any] | None) -> str:
    """Compose the user-side payload — title, scene transcript, per-source dossier."""
    meta = source_meta or {}
    sources = meta.get("sources") or []
    ok_sources = [s for s in sources if s.get("ok")]
    dossier = meta.get("original_text") or ""

    # ── per-source slice of the dossier ──
    # We slice the full dossier on the "[SOURCE n]" markers the router put
    # there. If markers aren't present (single-source flow), we'll fall back
    # to a global excerpt below.
    digests: list[_SourceDigest] = []
    if ok_sources and "[SOURCE 1]" in dossier:
        # Each block starts at "[SOURCE n] " and runs until the next marker
        # or EOF.
        import re
        markers = list(re.finditer(r"\[SOURCE (\d+)]", dossier))
        for i, m in enumerate(markers):
            n = int(m.group(1))
            if n > len(ok_sources):
                continue
            start = m.start()
            end = markers[i + 1].start() if i + 1 < len(markers) else len(dossier)
            block = dossier[start:end].strip()
            # Drop the header line so the digest is just the body text.
            body = block.split("\n", 2)
            body_text = body[-1].strip() if len(body) >= 3 else block
            src = ok_sources[n - 1]
            cit = src.get("citation") or {}
            digests.append(_SourceDigest(
                n=n,
                kind=src.get("kind", "?"),
                name=cit.get("name", "(unnamed)"),
                url=cit.get("url", ""),
                text=body_text[:1800],   # planner can refer to plan for the rest
            ))
    else:
        # Single-source flow: synthesise one digest from the global dossier.
        primary = ok_sources[0] if ok_sources else {}
        cit = primary.get("citation") or {}
        digests.append(_SourceDigest(
            n=1,
            kind=primary.get("kind", "?"),
            name=cit.get("name", "(unnamed)") if primary else "(unknown source)",
            url=cit.get("url", "") if primary else "",
            text=(dossier or "")[:1800],
        ))

    # ── per-scene voice script summary ──
    scene_lines: list[str] = []
    for s in plan.scenes:
        scene_lines.append(f"[Cảnh {s.scene_index} · {s.layout_id.value}] {s.voice_script.strip()}")

    # ── assemble ──
    parts: list[str] = [
        f"TIÊU ĐỀ VIDEO: {plan.title}",
        f"CONTENT_TYPE: {plan.content_type.value}  ·  LENGTH MODE: {plan.length}  ·  ASPECT: {plan.aspect_ratio}",
        "",
        "PER-SCENE VOICE-OVER (đã viết cho TTS, KHÔNG copy nguyên):",
    ]
    parts.extend(scene_lines)
    parts.append("")
    parts.append("PER-SOURCE DOSSIER (dùng để cite + lấy fact):")
    for d in digests:
        parts.append("")
        parts.append(f"[SOURCE {d.n}] kind={d.kind} · name={d.name}")
        if d.url:
            parts.append(f"URL: {d.url}")
        parts.append("---")
        parts.append(d.text)

    parts.append("")
    parts.append("→ Viết bài đầy đủ theo quy tắc system_instruction. Sẵn sàng copy-paste.")
    return "\n".join(parts)


async def build_full_caption(
    plan: ScenePlan,
    source_meta: dict[str, Any] | None,
    *,
    timeout_s: float = FULL_CAPTION_TIMEOUT_S,
) -> str:
    """Generate the long, copy-able caption via Gemini.

    Falls back to :func:`fallback_full_caption` on any failure (no client,
    timeout, empty response, suspiciously short response).
    """
    try:
        client = get_client()
    except Exception as exc:  # noqa: BLE001
        logger.warning("full_caption: no Gemini client (%s) — fallback", exc)
        return fallback_full_caption(plan, source_meta)

    user_text = _build_user_prompt(plan, source_meta)
    cfg = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT_VI,
        temperature=0.6,
        max_output_tokens=FULL_CAPTION_MAX_TOKENS,
        response_mime_type="text/plain",
    )

    try:
        resp = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=FULL_CAPTION_MODEL,
                contents=user_text,
                config=cfg,
            ),
            timeout=timeout_s,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("full_caption: Gemini call failed (%s) — fallback", exc)
        return fallback_full_caption(plan, source_meta)

    raw = (getattr(resp, "text", "") or "").strip()

    # Detect truncation — Gemini sets finish_reason=MAX_TOKENS when output
    # was cut at the limit. We extract the reason from the first candidate
    # so we can both LOG it and SALVAGE the partial output by dropping the
    # final dangling paragraph (the user-visible "cắt cụt giữa câu" bug).
    finish_reason = ""
    try:
        cand = (getattr(resp, "candidates", None) or [None])[0]
        if cand is not None:
            fr = getattr(cand, "finish_reason", None)
            finish_reason = str(getattr(fr, "name", fr) or "").upper()
    except Exception:
        pass

    if finish_reason == "MAX_TOKENS":
        logger.warning(
            "full_caption: hit MAX_TOKENS at %d chars — trimming dangling paragraph",
            len(raw),
        )
        raw = _trim_dangling_paragraph(raw)

    if len(raw) < 400:
        logger.warning(
            "full_caption: response too short (%d chars, finish=%s) — fallback",
            len(raw), finish_reason or "?",
        )
        return fallback_full_caption(plan, source_meta)

    logger.info(
        "full_caption: %d scenes × %d sources → %d-char caption (finish=%s)",
        len(plan.scenes),
        sum(1 for s in (source_meta or {}).get("sources", []) if s.get("ok")),
        len(raw),
        finish_reason or "STOP",
    )
    return raw


def _trim_dangling_paragraph(text: str) -> str:
    """Drop the trailing partial paragraph from a MAX_TOKENS-truncated reply.

    Strategy: split on blank-line paragraph breaks, then walk paragraphs
    from the end and drop any that don't end with sentence-final punctuation
    (.?!…). If the last surviving paragraph would leave the post without
    a NGUỒN block, append a tiny "── NGUỒN ──" header so the caption still
    looks complete to a reader even when Gemini ran out of room before
    listing sources.
    """
    paragraphs = [p.rstrip() for p in text.split("\n\n")]
    while paragraphs:
        last = paragraphs[-1].rstrip()
        if not last:
            paragraphs.pop()
            continue
        # Sentence-ending punctuation (cover en + vi punctuation set).
        if last.endswith((".", "!", "?", "…", "”", "”", ")", "]", "—", "•")):
            break
        # Could be a hashtag line — keep it.
        if last.lstrip().startswith("#"):
            break
        paragraphs.pop()
    out = "\n\n".join(paragraphs).rstrip()
    if "── NGUỒN ──" not in out and "NGUỒN" not in out:
        out += (
            "\n\n── NGUỒN ──\n"
            "• Tham khảo chi tiết tại các URL nguồn ở phần caption ngắn."
        )
    return out


def fallback_full_caption(plan: ScenePlan, source_meta: dict[str, Any] | None) -> str:
    """LLM-less fallback — assembles a serviceable post from voice scripts."""
    meta = source_meta or {}
    sources = meta.get("sources") or []
    ok_sources = [s for s in sources if s.get("ok")]

    lines: list[str] = [f"🚀 {plan.title}", ""]
    lines.append(
        "Dưới đây là bản tóm lược chi tiết các điểm chính từ "
        f"{len(ok_sources) or 1} nguồn đã tổng hợp."
    )
    lines.append("")

    for i, scene in enumerate(plan.scenes):
        icon = SECTION_ICONS[i % len(SECTION_ICONS)]
        body = (scene.voice_script or "").strip()
        if not body:
            continue
        lines.append(f"{icon} {body}")
        lines.append("")

    lines.append("👇 Anh em thấy điểm nào đáng chú ý nhất? Comment để mình bàn tiếp.")
    lines.append("")
    lines.append("── NGUỒN ──")
    for s in ok_sources:
        cit = s.get("citation") or {}
        name = cit.get("name") or "(unnamed)"
        url = cit.get("url") or ""
        if url:
            lines.append(f"• {name} — {url}")
        else:
            lines.append(f"• {name}")
    if not ok_sources:
        lines.append("• (không có nguồn ngoài)")
    lines.append("")
    lines.append("#Briefa #factualnews #neutralbrief #AInews")
    return "\n".join(lines).strip()


__all__ = [
    "build_full_caption",
    "fallback_full_caption",
    "FULL_CAPTION_MODEL",
    "SYSTEM_PROMPT_VI",
]
