"""English-word phonetic replacement for Vietnamese Edge TTS.

Edge TTS vi-VN voices cannot pronounce English terms naturally — they read
each Latin character through Vietnamese phonemes ("YouTube" → "iu tib",
"LLM" → "lờ lờ mờ").

This module rewrites the voice script BEFORE it reaches the TTS provider so
common English tech terms get a Vietnamese phonetic approximation that the
VN voice reads closer to the original English. The subtitle text is left
untouched (the planner's original output ships on screen).

Usage::

    from briefa.tts.phonetic import apply_en_phonetics
    voice_text = apply_en_phonetics(scene.voice_script)
"""
from __future__ import annotations

import re

# Phonetic map — Vietnamese approximation of English pronunciation.
# Longer keys are matched first to avoid prefix collisions ("ChatGPT" before
# "GPT").
EN_PHONETIC_MAP: dict[str, str] = {
    # ── Brand / product names ──
    "ChatGPT":       "chát gi-pi-ti",
    "OpenAI":        "ô-pần ây-ai",
    "Anthropic":     "an-thrô-pích",
    "Claude":        "cờ-lau-đì",
    "claude":        "cờ-lau-đì",
    "Generative":    "gien-ne-rây-tịp",
    "generative":    "gien-ne-rây-tịp",
    "Perplexity":    "pơ-pờ-lếc-xi-đì",
    "perplexity":    "pơ-pờ-lếc-xi-đì",
    "Overviews":     "âu-vờ-viu-s",
    "overviews":     "âu-vờ-viu-s",
    "Overview":      "âu-vờ-viu",
    "overview":      "âu-vờ-viu",
    "AI Overviews":  "ây-ai âu-vờ-viu-s",
    "Traffic":       "tra-phíc",
    "traffic":       "tra-phíc",
    "Auto":          "au-tu",
    "auto":          "au-tu",
    "Optimization":  "óp-ti-mai-zây-sừn",
    "optimization":  "óp-ti-mai-zây-sừn",
    "Optimizations": "óp-ti-mai-zây-sừn-s",
    "optimizations": "óp-ti-mai-zây-sừn-s",
    "Optimize":      "óp-ti-mai",
    "optimize":      "óp-ti-mai",
    "Citation":      "sai-tây-sừn",
    "citation":      "sai-tây-sừn",
    "Citations":     "sai-tây-sừn-s",
    "citations":     "sai-tây-sừn-s",
    "Citability":    "sai-tơ-bi-li-ti",
    "citability":    "sai-tơ-bi-li-ti",
    "MarkItDown":    "mác kít đao",
    "markitdown":    "mác kít đao",
    "Markdown":      "mác đao",
    "markdown":      "mác đao",
    "MarkDown":      "mác đao",
    "mark down":     "mác đao",
    "GitHub":        "ghít-hớp",
    "github.com":    "ghít-hớp chấm com",
    "Github":        "ghít-hớp",
    "GitLab":        "ghít-láp",
    "YouTube":       "iu túp",
    "youtube":       "iu túp",
    "TikTok":        "tích-tốc",
    "Facebook":      "phây-bóc",
    "Instagram":     "in-sờ-ta-gram",
    "LinkedIn":      "linh-cờ-din",
    "Twitter":       "tuýt-tơ",
    "Reddit":        "rê-đít",
    "Discord":       "đít-cót",
    "Slack":         "sờ-lác",
    "Microsoft":     "mai-cờ-rô-sốp",
    "Google":        "gu-gồ",
    "Apple":         "áp-pồ",
    "Meta":          "mê-ta",
    "Amazon":        "a-ma-zôn",
    "Netflix":       "nét-phờ-líc",
    "Spotify":       "sờ-pô-ti-phai",
    "Cursor":        "cơ-sờ",
    "VSCode":        "vi-ét-cốt",
    "Notion":        "nô-shần",
    "Figma":         "phíc-ma",
    "Vercel":        "vơ-xeo",
    "Cloudflare":    "cờ-lao-phờ-le",
    "AWS":           "ây đáp-bồ-iu ét",
    "Azure":         "a-giu-rờ",

    # ── Languages / frameworks ──
    "JavaScript":    "java-sờ-cờ-rip",
    "TypeScript":    "tai-sờ-cờ-rip",
    "Python":        "pai-thon",
    "Node.js":       "nốt giây-ét",
    "Node":          "nốt",
    "React":         "ri-ác",
    "Vue":           "viu",
    "Angular":       "ang-gu-lờ",
    "Django":        "den-gô",
    "Flask":         "phờ-lát",
    "Rails":         "rêu",
    "Spring":        "sờ-pring",
    "Docker":        "đốc-cờ",
    "Kubernetes":    "ku-bơ-nét",
    "FastAPI":       "phát ây-pi-ai",

    # ── Acronyms (read letter-by-letter) ──
    # LLM intentionally NOT mapped — Edge TTS VN reads "L L M" closest to
    # the English "el-el-em" on its own (A/B tested).
    "AI":            "ây-ai",
    "API":           "ây-pi-ai",
    "APIs":          "ây-pi-ai",
    "GPT":           "gi-pi-ti",
    "JSON":          "giây-sờn",
    "XML":           "ích em eo",
    "YAML":          "y-a-mồ",
    "CSV":           "xi ét vê",
    "CSS":           "xi ét ét",
    "HTML":          "hát ti em eo",
    "PHP":           "pi hát pi",
    "SQL":           "ét-qu-eo",
    "URL":           "u-ơ-eo",
    "URI":           "u-ơ-ai",
    "HTTP":          "hát ti ti pi",
    "HTTPS":         "hát ti ti pi ét",
    "REST":          "rét",
    "CRUD":          "cờ-rúp",
    "OAuth":         "ô-ốt",
    "JWT":           "giây đáp-bồ-iu ti",
    "PR":            "pi a",
    "MR":            "em a",
    "CI":            "xi-ai",
    "CD":            "xi-đi",
    "DevOps":        "đép-ốp",
    "SDK":           "ét-đi-kây",
    "IDE":           "ai-đi-ơ",
    "OS":            "âu ét",
    "iOS":           "ai-âu-ét",
    "macOS":         "mác-âu-ét",
    "CLI":           "xi-eo-ai",
    "GUI":           "gu-i",
    "UI":            "iu-ai",
    "UX":            "iu-ích",
    "ML":            "em eo",
    "NLP":           "en eo pi",
    "RAG":           "rớc",
    "MCP":           "em xi pi",
    "TPU":           "ti-pi-iu",
    "GPU":           "gi-pi-iu",
    "CPU":           "xi-pi-iu",
    "RAM":           "ram",
    "SSD":           "ét ét đi",
    "USB":           "iu ét bi",
    "DNS":           "đi en ét",
    "VPN":           "vi-pi-en",
    "VPS":           "vi-pi-ét",
    "SaaS":          "sát",
    "PaaS":          "pát",
    "B2B":           "bi-tu-bi",
    "B2C":           "bi-tu-xi",
    "ROI":           "rô-ai",
    "KPI":           "kây-pi-ai",
    "CEO":           "xi-i-âu",
    "CTO":           "xi-ti-âu",
    "CMO":           "xi-em-âu",

    # ── Common tech verbs / nouns ──
    "deploy":        "đi-plôi",
    "deploys":       "đi-plôi",
    "deployment":    "đi-plôi-mần",
    "commit":        "com-mít",
    "commits":       "com-mít",
    "merge":         "mơ-giờ",
    "branch":        "bran",
    "pull":          "pun",
    "push":          "puch",
    "repo":          "ri-pô",
    "repos":         "ri-pô",
    "repository":    "ri-pô-si-to-ri",
    "release":       "ri-lít",
    "releases":      "ri-lít",
    "build":         "biu",
    "builds":        "biu",
    "framework":     "phờ-rêm-guốc",
    "library":       "lai-bra-ri",
    "libraries":     "lai-bra-ri",
    "package":       "pác-cát",
    "packages":      "pác-cát",
    "module":        "mô-giun",
    "modules":       "mô-giun",
    "function":      "phấng-shần",
    "functions":     "phấng-shần",
    "callback":      "côn-bác",
    "async":         "ây-sinh",
    "await":         "ơ-weit",
    "stream":        "sờ-trim",
    "streaming":     "sờ-trim-mình",
    "token":         "tâu-cần",
    "tokens":        "tâu-cần",
    "prompt":        "prom",
    "prompts":       "prom",
    "agent":         "ây-giần",
    "agents":        "ây-giần",
    "model":         "mô-đồ",
    "models":        "mô-đồ",
    "endpoint":      "en-poi",
    "endpoints":     "en-poi",
    "schema":        "sờ-kê-ma",
    "workflow":      "guốc-phờ-lâu",
    "pipeline":      "pai-pờ-lai",
    "dashboard":     "đát-bót",
    "feature":       "phít-trờ",
    "features":      "phít-trờ",

    # ── Vietnamese-mixed tech terms ──
    "Hugging Face":  "ha-ghing-phây",
    "fine-tune":     "phai-tiu",
    "fine tune":     "phai-tiu",
    "fine-tuning":   "phai-tiu-nình",
    "open source":   "ô-pần sót",
    "open-source":   "ô-pần-sót",
    "low-code":      "lâu-cốt",
    "no-code":       "nâu-cốt",
}


# Split keys by case-sensitivity policy:
#   - ALL-UPPERCASE acronyms (LLM, GPT, ROI, AI…) need case-sensitive matching
#     so we don't clobber Vietnamese words ("roi" = whip, "ai" = who).
#   - Mixed-case and lowercase keys (Microsoft, GitHub, markitdown) get
#     case-insensitive matching so any user-written variant hits.
_UPPER_KEYS = sorted(
    [k for k in EN_PHONETIC_MAP if k.isupper() and k.isascii()],
    key=lambda k: -len(k),
)
_MIXED_KEYS = sorted(
    [k for k in EN_PHONETIC_MAP if not (k.isupper() and k.isascii())],
    key=lambda k: -len(k),
)
_LOWER_TO_VI = {k.lower(): v for k, v in EN_PHONETIC_MAP.items()}

# Lookahead allows ``.`` only if not followed by a letter, so ``markitdown.``
# matches but ``Node.js`` matches as the full key (not as "Node" alone).
_LOOKAHEAD = r"(?!(?:[A-Za-z0-9_]|\.[A-Za-z]))"
_LOOKBEHIND = r"(?<![A-Za-z0-9])"

_UPPER_RE = (
    re.compile(_LOOKBEHIND + r"(" + "|".join(re.escape(k) for k in _UPPER_KEYS) + r")" + _LOOKAHEAD)
    if _UPPER_KEYS else None
)
_MIXED_RE = (
    re.compile(
        _LOOKBEHIND + r"(" + "|".join(re.escape(k) for k in _MIXED_KEYS) + r")" + _LOOKAHEAD,
        re.IGNORECASE,
    )
    if _MIXED_KEYS else None
)


def apply_en_phonetics(text: str) -> str:
    """Replace English tech terms with Vietnamese phonetic approximations.

    Two passes: case-sensitive uppercase acronyms first (to avoid clobbering
    ``ai`` / ``roi`` etc.), then case-insensitive mixed-case keys.
    """
    if not text:
        return text

    def _sub_upper(m: re.Match) -> str:
        return EN_PHONETIC_MAP[m.group(1)]

    def _sub_mixed(m: re.Match) -> str:
        return _LOWER_TO_VI.get(m.group(1).lower(), m.group(1))

    if _UPPER_RE is not None:
        text = _UPPER_RE.sub(_sub_upper, text)
    if _MIXED_RE is not None:
        text = _MIXED_RE.sub(_sub_mixed, text)
    return text


__all__ = ["apply_en_phonetics", "EN_PHONETIC_MAP"]
