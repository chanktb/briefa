"""Input source normalizers — text, article URL, GitHub repo, image.

Legacy ktb-ai-news entry point :func:`detect_and_normalize` still works for
single-input flows. Briefa's N-mix-input dispatcher is :func:`route_and_merge`
in :mod:`briefa.sources.router` — see ``DECISIONS.md`` D005.
"""

from .github import (
    fetch_github_stats,
    fetch_readme,
    fetch_recent_commits,
    fetch_repo_full,
    format_github_stats_block,
    format_repo_full_block,
    parse_repo_url,
)
from .multi_source import SourceFetch, fetch_multi
from .router import (
    BriefaInput,
    CitationChip,
    InputKind,
    detect_kind,
    merge_to_dossier,
    route_and_merge,
    route_inputs,
)
from .url_extract import (
    DOMAIN_FRIENDLY_NAME,
    FetchResult,
    SourceType,
    detect_and_normalize,
    extract_domain,
    extract_url_from_text,
    fetch_url,
    friendly_source_name,
)
from .vision import VISION_SYSTEM_PROMPT, extract_from_images

__all__ = [
    # legacy ktb-ai-news exports (kept for back-compat with renderer/composer)
    "SourceType",
    "FetchResult",
    "DOMAIN_FRIENDLY_NAME",
    "friendly_source_name",
    "extract_domain",
    "extract_url_from_text",
    "fetch_url",
    "detect_and_normalize",
    "parse_repo_url",
    "fetch_github_stats",
    "format_github_stats_block",
    "extract_from_images",
    "VISION_SYSTEM_PROMPT",
    "SourceFetch",
    "fetch_multi",
    # Briefa extensions (Phase 1 — see DECISIONS D005)
    "fetch_readme",
    "fetch_recent_commits",
    "fetch_repo_full",
    "format_repo_full_block",
    "InputKind",
    "CitationChip",
    "BriefaInput",
    "detect_kind",
    "route_inputs",
    "merge_to_dossier",
    "route_and_merge",
]
