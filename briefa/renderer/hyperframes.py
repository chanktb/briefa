"""HyperFrames CLI wrapper — turn a rendered HTML project into an MP4.

HyperFrames (https://github.com/krmcfarlane/hyperframes) renders an HTML file
through headless Chrome (via Puppeteer) and produces a vertical MP4. We shell
out to ``npx hyperframes@<version>`` rather than vendoring the JS toolchain.

The composer writes the per-job HyperFrames project (``index.html``,
``package.json``, ``hyperframes.json``, ``meta.json``, ``manifest.json``)
into ``job_dir``. This module:

  - :func:`write_project_files` — emit the static project files
  - :func:`render` — run ``npx hyperframes render`` and return the MP4 path
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("briefa.renderer.hyperframes")

HYPERFRAMES_VERSION_DEFAULT = "0.6.52"
VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
VIDEO_FPS = 30


@dataclass
class RenderResult:
    """Outcome of a HyperFrames render run."""
    mp4_path: Path
    stdout: str
    stderr: str
    returncode: int

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.mp4_path.exists() and self.mp4_path.stat().st_size > 0


def write_project_files(
    *,
    job_dir: Path,
    project_id: str,
    title: str,
    total_duration: float,
    hyperframes_version: str = HYPERFRAMES_VERSION_DEFAULT,
    width: int = VIDEO_WIDTH,
    height: int = VIDEO_HEIGHT,
    fps: int = VIDEO_FPS,
) -> None:
    """Write the static HyperFrames project files into ``job_dir``.

    The composer is expected to have already written ``index.html`` (the
    Jinja-rendered composition) and any ``assets/`` / ``static/`` payloads.
    """
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)

    package_json = {
        "name": project_id,
        "private": True,
        "version": "1.0.0",
        "scripts": {
            "render": f"npx --yes hyperframes@{hyperframes_version} render",
        },
    }
    (job_dir / "package.json").write_text(
        json.dumps(package_json, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    meta_json = {"id": project_id, "name": title}
    (job_dir / "meta.json").write_text(
        json.dumps(meta_json, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    hyperframes_json = {
        "version": hyperframes_version,
        "composition": "main",
        "input": "index.html",
        "output": "output.mp4",
        "width": width,
        "height": height,
        "fps": fps,
        "duration": total_duration,
    }
    (job_dir / "hyperframes.json").write_text(
        json.dumps(hyperframes_json, indent=2, ensure_ascii=False), encoding="utf-8"
    )


async def render(
    job_dir: Path,
    *,
    hyperframes_version: str = HYPERFRAMES_VERSION_DEFAULT,
    npx_bin: str = "npx",
    timeout: float = 600.0,
) -> RenderResult:
    """Run ``npx hyperframes render`` inside ``job_dir``.

    HyperFrames 0.6.x writes the MP4 to ``renders/<project>_<timestamp>.mp4``
    rather than the ``output`` field from ``hyperframes.json``. We pick the
    most recently modified ``.mp4`` under ``renders/`` after the run.

    Args:
        job_dir:             Directory holding the project files.
        hyperframes_version: Pinned npm version of the CLI.
        npx_bin:             Override the ``npx`` executable path.
        timeout:             Render timeout in seconds.

    Raises:
        TimeoutError: if hyperframes does not return within ``timeout``.
    """
    job_dir = Path(job_dir)
    renders_dir = job_dir / "renders"

    # asyncio.create_subprocess_exec on Windows uses CreateProcess directly,
    # which skips the .cmd/.bat suffix search that cmd.exe applies — so a
    # bare "npx" raises WinError 2 even when npx.cmd is on PATH. Resolve
    # to the full path here so the same code works on Linux (npx) and
    # Windows (npx.cmd). On Linux shutil.which returns the bare path.
    import shutil as _shutil
    resolved = _shutil.which(npx_bin) or npx_bin

    cmd = [resolved, "--yes", f"hyperframes@{hyperframes_version}", "render"]
    logger.info("hyperframes render in %s (%s)", job_dir, " ".join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(job_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    # ── Drain stdout/stderr concurrently with proc.wait() ──
    # Earlier we used ``asyncio.wait_for(proc.communicate(), timeout=...)``
    # which deadlocked on real-world runs: HyperFrames subprocess exited
    # cleanly (final mp4 written, all Chrome / ffmpeg children reaped) but
    # ``communicate()`` never returned and the 900 s timeout never fired
    # either, leaving the autopilot daemon stuck for 30+ minutes.
    #
    # The robust pattern is: spawn manual reader tasks for each pipe and
    # await ``proc.wait()`` directly. The pipes naturally close after the
    # subprocess exits, the readers finish, and we never depend on the
    # internal state of asyncio's StreamReader cleanup machinery — which
    # is what (apparently) gets confused when HF's npx wrapper exits.
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []

    async def _drain(stream: asyncio.StreamReader | None, sink: list[bytes]) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            sink.append(chunk)

    drain_out = asyncio.create_task(_drain(proc.stdout, stdout_chunks))
    drain_err = asyncio.create_task(_drain(proc.stderr, stderr_chunks))

    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=10)
        drain_out.cancel()
        drain_err.cancel()
        raise TimeoutError(
            f"hyperframes render timed out after {timeout:.0f}s in {job_dir}"
        ) from None

    # Subprocess exited — drainers should finish almost immediately as the
    # pipes are closed. Cap with a small timeout so a buggy pipe state
    # can't keep us hung anyway.
    try:
        await asyncio.wait_for(asyncio.gather(drain_out, drain_err), timeout=30)
    except TimeoutError:
        drain_out.cancel()
        drain_err.cancel()
        logger.warning("hyperframes drainer timeout — proceeding with partial output")

    stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace")
    stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    returncode = proc.returncode if proc.returncode is not None else -1

    # Locate the produced MP4. HyperFrames stamps each render with a timestamp
    # so we take the newest .mp4 under renders/ — empty Path if none exists.
    mp4_path = job_dir / "output.mp4"  # placeholder; overwritten below if found
    if renders_dir.is_dir():
        candidates = sorted(
            (p for p in renders_dir.glob("*.mp4") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            mp4_path = candidates[0]

    if returncode != 0:
        logger.warning(
            "hyperframes returncode=%d stderr=%s", returncode, stderr[-500:]
        )

    return RenderResult(
        mp4_path=mp4_path,
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
    )


__all__ = [
    "HYPERFRAMES_VERSION_DEFAULT",
    "VIDEO_WIDTH",
    "VIDEO_HEIGHT",
    "VIDEO_FPS",
    "RenderResult",
    "write_project_files",
    "render",
]
