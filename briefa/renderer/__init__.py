"""HTML composer + HyperFrames wrapper → MP4.

High-level usage::

    from briefa.renderer import compose, render

    index_html = await compose(scene_plan, channel_config, job_dir)
    result = await render(job_dir)
    if result.ok:
        ...  # result.mp4_path is the finished MP4

The :func:`compose` step writes ``index.html`` + asset payloads + the
HyperFrames project files; :func:`render` runs the HyperFrames CLI to turn
``index.html`` into ``output.mp4``.
"""

from .composer import (
    LAYOUT_FILENAMES,
    compose,
    compute_bullet_timings,
    split_voice_into_segments,
)
from .hyperframes import (
    HYPERFRAMES_VERSION_DEFAULT,
    VIDEO_FPS,
    VIDEO_HEIGHT,
    VIDEO_WIDTH,
    RenderResult,
    render,
    write_project_files,
)

__all__ = [
    "compose",
    "render",
    "RenderResult",
    "write_project_files",
    "HYPERFRAMES_VERSION_DEFAULT",
    "VIDEO_WIDTH",
    "VIDEO_HEIGHT",
    "VIDEO_FPS",
    "split_voice_into_segments",
    "compute_bullet_timings",
    "LAYOUT_FILENAMES",
]
