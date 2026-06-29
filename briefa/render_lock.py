"""Cross-bot render mutex — file lock that serialises HyperFrames renders.

HyperFrames runs a headless Chromium that screenshots every frame; on a
4-vCPU VPS each render is single-thread CPU bound. Two parallel renders
each take ~2× as long without delivering any extra throughput, so we
funnel every render — VCM bot, AINews bot, NewsEditor bot, VCM
autopilot — through a single lock.

Lock semantics
--------------
- **Interactive bots** (user typed ``/render``) acquire with a long
  timeout. While they wait, an ``on_wait`` callback fires every ~30 s so
  the bot can ping Telegram with an updated ETA — "đang đợi job khác,
  còn khoảng N phút".
- **Autopilot** acquires non-blocking. If the lock is held it raises
  :class:`RenderBusy` and the scheduler skips this tick (next wake-up is
  10 minutes away anyway).

The lock file path is overridable via ``KTB_RENDER_LOCK_PATH`` so local
dev / tests can point it at a tmp file instead of ``/tmp/ktb-studio-
render.lock``. Holder info (name, PID, started_at) is written into the
file so the on-wait callback can show the user *what* is blocking.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

logger = logging.getLogger("briefa.render_lock")

# Default lock file location. ``/var/lib/ktb-studio/render.lock`` works
# across systemd units that set ``PrivateTmp=true`` (which our 4 prod
# bots do — /tmp is private per-unit so a /tmp lock would NOT actually
# coordinate between services). /var/lib stays shared across the system.
#
# The setup script ``deploy/scripts/setup_nginx_videos.sh`` creates the
# parent dir (or run ``sudo mkdir /var/lib/ktb-studio && sudo chown
# ktb:ktb /var/lib/ktb-studio`` once by hand). On Windows / local dev,
# point ``KTB_RENDER_LOCK_PATH`` at a temp file.
_DEFAULT_LOCK_PATH = "/var/lib/ktb-studio/render.lock"


def _lock_path() -> Path:
    return Path(os.environ.get("KTB_RENDER_LOCK_PATH", _DEFAULT_LOCK_PATH))


# Platform-specific locking primitives. We try fcntl on POSIX (proper
# advisory lock recognised across processes) and msvcrt.locking on
# Windows (best-effort — covers Windows local dev so the lock file is
# at least exclusive within a single machine).
if sys.platform == "win32":  # pragma: no cover - dev shim
    import msvcrt

    def _try_lock(fd: int) -> bool:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (BlockingIOError, OSError):
            return False

    def _unlock(fd: int) -> None:
        with suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)


# ════════════════════════════════════════════════════════════════════════
# Exceptions + holder helpers
# ════════════════════════════════════════════════════════════════════════

class RenderBusy(Exception):
    """The render lock could not be acquired within the allotted time."""

    def __init__(self, holder: dict[str, Any]):
        self.holder = holder
        name = holder.get("name", "?")
        pid = holder.get("pid", "?")
        elapsed = holder.get("elapsed_s", 0)
        super().__init__(
            f"Render lock held by {name} (PID {pid}, running {elapsed}s)"
        )


def _read_holder() -> dict[str, Any]:
    """Best-effort read of the current lock holder's metadata.

    Returns a dict with keys ``name``, ``pid``, ``started_at``,
    ``elapsed_s``. Missing/unparseable file → empty dict.
    """
    try:
        raw = _lock_path().read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return {}
    if not raw:
        return {}
    parts = raw.split("|")
    if len(parts) < 3:
        return {"name": raw, "pid": 0, "started_at": 0, "elapsed_s": 0}
    name, pid_s, ts_s = parts[0], parts[1], parts[2]
    try:
        pid = int(pid_s)
        started_at = int(ts_s)
    except ValueError:
        return {"name": name, "pid": 0, "started_at": 0, "elapsed_s": 0}
    return {
        "name": name,
        "pid": pid,
        "started_at": started_at,
        "elapsed_s": max(0, int(time.time()) - started_at),
    }


def current_holder() -> dict[str, Any]:
    """Public helper — same as :func:`_read_holder` for diagnostics."""
    return _read_holder()


# ════════════════════════════════════════════════════════════════════════
# Async context manager
# ════════════════════════════════════════════════════════════════════════

OnWaitCb = Callable[[dict[str, Any]], Awaitable[None] | None]


@asynccontextmanager
async def render_slot(
    name: str,
    *,
    timeout: float = 0.0,
    on_wait: OnWaitCb | None = None,
    wait_notify_interval_s: float = 30.0,
    poll_interval_s: float = 2.0,
):
    """Acquire the global render lock.

    Args:
        name: Short identifier for the caller — e.g. ``"vcm-bot"``,
              ``"vcm-autopilot"``, ``"ainews-bot"``. Written into the
              lock file so other callers can show "đang đợi <name>" to
              their users.
        timeout: ``0.0`` (default) = non-blocking. The lock is either
                 free now or :class:`RenderBusy` is raised immediately.
                 ``> 0`` = block up to ``timeout`` seconds, polling every
                 ``poll_interval_s``.
        on_wait: Optional callable invoked every ``wait_notify_interval_s``
                 while we're blocked, with the current holder's metadata
                 dict. May be sync or ``async``. Use it to send a "đang
                 đợi" message to Telegram. Exceptions are swallowed so a
                 misbehaving notifier never breaks render scheduling.
        wait_notify_interval_s: Seconds between ``on_wait`` calls.
        poll_interval_s: Seconds between lock retries while blocking.

    Raises:
        RenderBusy: timeout=0 and lock held, or timeout elapsed while
                    blocking. ``.holder`` carries the blocker's info.
    """
    lock_path = _lock_path()
    # Make sure the parent dir exists — /tmp always does, but a caller
    # might point KTB_RENDER_LOCK_PATH at a nested location.
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # O_CREAT keeps the file across processes; O_RDWR lets us write our
    # holder info once we win. We deliberately do NOT use O_TRUNC here
    # because that would race against the current holder (another process
    # might have just written its holder info).
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    deadline = (time.time() + timeout) if timeout > 0 else None
    last_notify = 0.0

    try:
        # ── Acquire loop ──
        while True:
            if _try_lock(fd):
                # Won — overwrite file with our holder line.
                os.lseek(fd, 0, os.SEEK_SET)
                os.ftruncate(fd, 0)
                line = f"{name}|{os.getpid()}|{int(time.time())}\n".encode()
                os.write(fd, line)
                logger.info("render_slot acquired by %s (pid=%d)", name, os.getpid())
                break

            # Couldn't get it. Decide: bail immediately or keep trying.
            now = time.time()
            if deadline is None:
                holder = _read_holder()
                logger.info(
                    "render_slot busy → caller=%s holder=%s",
                    name, holder.get("name") or "?",
                )
                raise RenderBusy(holder)
            if now >= deadline:
                holder = _read_holder()
                logger.warning(
                    "render_slot timeout after %.0fs (caller=%s holder=%s)",
                    timeout, name, holder.get("name") or "?",
                )
                raise RenderBusy(holder)

            # Notify-while-waiting hook (rate-limited).
            if on_wait and (now - last_notify) >= wait_notify_interval_s:
                last_notify = now
                try:
                    res = on_wait(_read_holder())
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:  # noqa: BLE001 — notifier must not break scheduling
                    logger.exception("render_slot on_wait raised; ignoring")

            await asyncio.sleep(poll_interval_s)

        # ── Hold the slot while the body runs ──
        yield
    finally:
        # Always release. We close the FD after unlocking; the file
        # itself stays at /tmp so the next acquirer can re-open it.
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
        except OSError:
            pass
        _unlock(fd)
        with suppress(OSError):
            os.close(fd)
        logger.info("render_slot released by %s", name)


__all__ = ["RenderBusy", "current_holder", "render_slot"]
