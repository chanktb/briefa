"""AI image generation — shared across tools that need illustrative imagery.

Currently exposes :mod:`core.images.pollinations` (free, no-key). If another
tool needs a faster provider, add it here with the same public surface
(``generate_image`` / ``generate_batch``).
"""

from .pollinations import (
    DEFAULT_HEIGHT,
    DEFAULT_MODEL,
    DEFAULT_MODEL_CHAIN,
    DEFAULT_STYLE_SUFFIX,
    DEFAULT_WIDTH,
    generate_image,
    write_placeholder,
)
from .provider import generate_batch

__all__ = [
    "DEFAULT_HEIGHT",
    "DEFAULT_MODEL",
    "DEFAULT_MODEL_CHAIN",
    "DEFAULT_STYLE_SUFFIX",
    "DEFAULT_WIDTH",
    "generate_batch",
    "generate_image",
    "write_placeholder",
]
