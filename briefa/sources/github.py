"""Fetch real stats + README + recent commits from the public GitHub REST API.

Used when an input URL points at a GitHub repo. The planner consumes the
formatted block (numbers + README excerpt + commit log) so it can describe
the repo using EXACT figures and ACTUAL recent activity rather than guess.

Briefa-specific extensions vs the ktb-ai-news origin:

  * ``fetch_readme``         — GET ``/repos/{o}/{r}/readme``, base64-decode.
  * ``fetch_recent_commits`` — GET ``/repos/{o}/{r}/commits?per_page=N``.
  * ``fetch_repo_full``      — composite: stats + readme + commits in one call.
  * ``format_repo_full_block`` — formatter that returns the full briefing block.

Auth: ``GITHUB_TOKEN`` env (optional). Without it the 60 req/hour anonymous
budget is plenty for on-demand video generation. With it the rate goes to
5 000 req/hour — useful when E2E tests fire many requests in a row.
"""
from __future__ import annotations

import base64
import logging
import os
import re

import httpx

logger = logging.getLogger("briefa.sources.github")

_GITHUB_REPO_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/([^/\s]+)/([^/\s#?]+)",
    re.IGNORECASE,
)

_USER_AGENT = "briefa/0.1 (+https://github.com/ktbteam/briefa)"
_API_TIMEOUT = 10.0
_README_MAX_CHARS = 2400          # truncate README for the planner context budget
_DEFAULT_COMMITS = 5              # how many recent commits to show by default


def _auth_headers() -> dict:
    """Common request headers; adds Authorization when GITHUB_TOKEN is set."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": _USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def parse_repo_url(url: str) -> tuple[str, str] | None:
    """Extract ``(owner, repo)`` from a GitHub repo URL.

    Returns ``None`` if the URL is not a repo-form GitHub URL.
    """
    m = _GITHUB_REPO_RE.match(url)
    if not m:
        return None
    owner = m.group(1)
    repo = m.group(2).rstrip(".git")
    return owner, repo


async def fetch_github_stats(url: str) -> dict | None:
    """Fetch repo stats from the GitHub REST API.

    Returns a dict of cherry-picked fields on success, or ``None`` if the
    URL is not a repo URL, the API call fails, or the repo doesn't exist.
    """
    parsed = parse_repo_url(url)
    if parsed is None:
        return None
    owner, repo = parsed
    api_url = f"https://api.github.com/repos/{owner}/{repo}"
    try:
        async with httpx.AsyncClient(timeout=_API_TIMEOUT) as client:
            r = await client.get(api_url, headers=_auth_headers())
            if r.status_code != 200:
                logger.debug("GitHub API %s returned %d", api_url, r.status_code)
                return None
            data = r.json()
    except httpx.HTTPError as exc:
        logger.debug("GitHub API request failed: %s", exc)
        return None

    owner_obj = data.get("owner") or {}
    return {
        "owner": owner_obj.get("login") or "",
        "owner_avatar_url": owner_obj.get("avatar_url") or "",
        "repo": data.get("name") or "",
        "full_name": data.get("full_name"),
        "description": data.get("description") or "",
        "stars": data.get("stargazers_count", 0),
        "forks": data.get("forks_count", 0),
        "watchers": data.get("watchers_count", 0),
        "open_issues": data.get("open_issues_count", 0),
        "language": data.get("language") or "",
        "topics": data.get("topics") or [],
        "license": (data.get("license") or {}).get("spdx_id") or "",
        "created_at": (data.get("created_at") or "")[:10],
        "pushed_at": (data.get("pushed_at") or "")[:10],
        "default_branch": data.get("default_branch") or "main",
        "homepage": data.get("homepage") or "",
    }


async def fetch_readme(url: str, *, max_chars: int = _README_MAX_CHARS) -> str | None:
    """Fetch the rendered README via ``/repos/{o}/{r}/readme`` and base64-decode.

    Returns the decoded README text truncated to ``max_chars`` so the planner
    context stays bounded. ``None`` if the URL is not a repo, the README does
    not exist, or the call fails.
    """
    parsed = parse_repo_url(url)
    if parsed is None:
        return None
    owner, repo = parsed
    api_url = f"https://api.github.com/repos/{owner}/{repo}/readme"
    try:
        async with httpx.AsyncClient(timeout=_API_TIMEOUT) as client:
            r = await client.get(api_url, headers=_auth_headers())
            if r.status_code != 200:
                logger.debug("GitHub README API %s returned %d", api_url, r.status_code)
                return None
            payload = r.json()
    except httpx.HTTPError as exc:
        logger.debug("GitHub README request failed: %s", exc)
        return None

    raw = payload.get("content") or ""
    encoding = (payload.get("encoding") or "base64").lower()
    if encoding != "base64":
        logger.debug("Unexpected README encoding: %s", encoding)
        return None
    try:
        decoded = base64.b64decode(raw).decode("utf-8", errors="replace").strip()
    except Exception as exc:
        logger.debug("README decode failed: %s", exc)
        return None
    if not decoded:
        return None
    # Strip noisy GitHub badges / HTML comments before truncating so the
    # truncated slice contains actual prose, not shield URLs.
    decoded = re.sub(r"<!--.*?-->", "", decoded, flags=re.DOTALL)
    decoded = re.sub(r"!\[[^\]]*]\([^\)]*shields\.io[^\)]*\)", "", decoded)
    decoded = re.sub(r"\n{3,}", "\n\n", decoded).strip()
    if len(decoded) > max_chars:
        decoded = decoded[:max_chars].rstrip() + "\n…(README truncated)"
    return decoded


async def fetch_recent_commits(url: str, *, n: int = _DEFAULT_COMMITS) -> list[dict]:
    """Fetch the last ``n`` commits via ``/repos/{o}/{r}/commits?per_page=N``.

    Returns a list of ``{sha, date, author, subject}`` dicts (newest first).
    Empty list on failure or non-repo URL.
    """
    parsed = parse_repo_url(url)
    if parsed is None:
        return []
    owner, repo = parsed
    api_url = f"https://api.github.com/repos/{owner}/{repo}/commits"
    try:
        async with httpx.AsyncClient(timeout=_API_TIMEOUT) as client:
            r = await client.get(
                api_url,
                params={"per_page": str(max(1, min(n, 30)))},
                headers=_auth_headers(),
            )
            if r.status_code != 200:
                logger.debug("GitHub commits API %s returned %d", api_url, r.status_code)
                return []
            raw = r.json() or []
    except httpx.HTTPError as exc:
        logger.debug("GitHub commits request failed: %s", exc)
        return []

    commits: list[dict] = []
    for item in raw:
        commit = (item or {}).get("commit") or {}
        author = commit.get("author") or {}
        message = (commit.get("message") or "").strip()
        subject = message.split("\n", 1)[0][:120] if message else ""
        commits.append(
            {
                "sha":     (item.get("sha") or "")[:7],
                "date":    (author.get("date") or "")[:10],
                "author":  author.get("name") or "",
                "subject": subject,
            }
        )
    return commits


async def fetch_repo_full(url: str, *, commit_count: int = _DEFAULT_COMMITS) -> dict | None:
    """Composite fetch: stats + README + recent commits in one call.

    Returns ``{stats, readme, commits}`` or ``None`` when the stats fetch
    itself fails. README / commits are best-effort and may be empty.
    """
    stats = await fetch_github_stats(url)
    if stats is None:
        return None
    readme = await fetch_readme(url)
    commits = await fetch_recent_commits(url, n=commit_count)
    return {"stats": stats, "readme": readme or "", "commits": commits}


# ────────────────────────────── formatters ──────────────────────────────


def _short_stars(stars: int) -> str:
    """Render stars as a short display string: ``134036`` → ``"134K"``."""
    if stars >= 1000:
        return f"{stars / 1000:.0f}K"
    return str(stars)


def format_github_stats_block(stats: dict) -> str:
    """Stats-only block (legacy ktb-ai-news shape).

    Kept for back-compat with the existing planner prompt which still
    references ``GITHUB_REPO_STATS``. New code paths should prefer
    :func:`format_repo_full_block`.
    """
    stars = stats.get("stars", 0)
    stars_short = _short_stars(stars)
    lines = [
        "═══════════════════════════════════════════════════════════════",
        "GITHUB_REPO_STATS (from official GitHub API — use these EXACT numbers):",
        "═══════════════════════════════════════════════════════════════",
        f"- Repo: {stats.get('full_name', '?')}",
        f"- Owner: {stats.get('owner', '?')}",
        f"- Owner avatar URL: {stats.get('owner_avatar_url', '')}",
        f"- Description: {stats.get('description', '')}",
        f"- Stars: {stars:,} (display short: {stars_short})",
        f"- Forks: {stats.get('forks', 0):,}",
        f"- Open issues: {stats.get('open_issues', 0):,}",
        f"- Primary language: {stats.get('language', '?')}",
        f"- Topics: {', '.join(stats.get('topics', [])[:8])}",
        f"- License: {stats.get('license', '?')}",
        f"- Created: {stats.get('created_at', '?')}",
        f"- Last push: {stats.get('pushed_at', '?')}",
    ]
    if stats.get("homepage"):
        lines.append(f"- Homepage: {stats.get('homepage')}")
    lines.append("")
    lines.append("Layout hints for planner:")
    lines.append("  - Use HeroCardWithLogo for scene 1 (logo_url = owner_avatar_url above).")
    lines.append("  - Use BigStatCard with big_value=stars_short ('134K'), big_unit='sao'.")
    lines.append("  - Use TerminalWindow if README mentions install/usage commands.")
    return "\n".join(lines)


def format_repo_full_block(repo: dict) -> str:
    """Full briefing block: stats + README excerpt + recent commits.

    Designed for Briefa's planner prompt — gives Gemini concrete facts to
    quote so it never invents repo features or activity.
    """
    stats = repo.get("stats") or {}
    readme = (repo.get("readme") or "").strip()
    commits = repo.get("commits") or []

    lines = [format_github_stats_block(stats), ""]

    if readme:
        lines += [
            "═══════════════════════════════════════════════════════════════",
            "GITHUB_REPO_README (excerpt from the rendered README.md):",
            "═══════════════════════════════════════════════════════════════",
            readme,
            "",
        ]

    if commits:
        lines += [
            "═══════════════════════════════════════════════════════════════",
            f"GITHUB_RECENT_COMMITS — last {len(commits)} commits (newest first):",
            "═══════════════════════════════════════════════════════════════",
        ]
        for c in commits:
            lines.append(
                f"  {c['date']}  {c['sha']}  {c['author']:<24.24}  {c['subject']}"
            )
        lines.append("")
        lines.append(
            "Use these commit lines verbatim when describing recent activity. "
            "Do NOT invent commits."
        )

    return "\n".join(lines).rstrip()


__all__ = [
    "parse_repo_url",
    "fetch_github_stats",
    "fetch_readme",
    "fetch_recent_commits",
    "fetch_repo_full",
    "format_github_stats_block",
    "format_repo_full_block",
]
