# Briefa

> **Factual briefing video — không sáng tác.**

Briefa biến **1-N inputs hỗn hợp** (URL báo + URL GitHub repo + plain text + ≤5 images) thành **1 video MP4 ≤120s** với content **neutral factual** và **citation chip per scene**.

## Status

🚧 **Standalone CLI build in progress** — Phase 0 đang đẻ skeleton. Xem `STATE.md`.

## Tại sao tồn tại

Tool thứ 2 trong umbrella SaaS **Shortcraft** (`app.khuetran.com`). Khác với Lingora (language learning), Briefa nằm ở góc **factual + neutral** — chuyên brief tin tức + repo + sự kiện.

System prompt strict *"không sáng tác"* — Gemini bị ép neutral editor mode. Nếu source thiếu info → output `"không đủ thông tin"` thay vì đoán.

## Phases

| Phase | Goal | Status |
|---|---|---|
| 0 | Folder skeleton + 5 docs nền tảng | 🔄 |
| 1 | `briefa/ingest/` 4 fetcher (article/github/text/vision) | ⏳ |
| 2 | `briefa/planner/` synthesize + strict factual prompt | ⏳ |
| 3 | `briefa/composer/` scene + citation chip data | ⏳ |
| 4 | `briefa/render/` MP4 pipeline + theme `neutral_news` | ⏳ |
| 5 | E2E test 3 case | ⏳ |

## CLI usage (Phase 5 target)

```bash
python -m briefa.render \
  --input "https://vnexpress.net/some-article" \
  --input "https://github.com/anthropics/courses" \
  --input "Tôi muốn nhấn mạnh đoạn data 5 năm" \
  --image ./chart.png \
  --theme neutral_news \
  --aspect 9:16 \
  --channel briefa-test \
  --output ./out/test.mp4
```

## Docs

- `CLAUDE_CONTEXT.md` — read first nếu mở session mới
- `STATE.md` — living state
- `DECISIONS.md` — append-only quyết định + why
- `BRIEF.md` — product spec đầy đủ
- `SESSIONS/` — diary mỗi session

## Tech stack

- Python 3.12
- `httpx + beautifulsoup4 + lxml + trafilatura` — article scrape
- `google-genai` — text synthesis + vision OCR
- `edge-tts>=7.0` (KHÔNG dùng 6.1.x — dead từ 2026) — primary TTS
- `gtts` — escape-hatch fallback (env `BRIEFA_ALLOW_GTTS=1`)
- Cloudflare Workers AI FLUX — image gen (5 accounts rotation)
- Pexels — stock photo fallback
- ffmpeg + Jinja2 HTML template — render layer

## Setup (Windows)

```powershell
cd D:\myworkspace\projects\briefa
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
copy .env.example .env
# Fill in GEMINI_API_KEYS (required), CLOUDFLARE_ACCOUNTS (optional), PEXELS_API_KEYS (optional)
```

## License

MIT — KTB Team
