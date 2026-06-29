"""Channel-level voice gender policies applied after planner output.

The planner already picks a gender per ``content_type`` (TECH / LEARNING
default male; NEWS / STORY pick randomly). That works for a general
content bot but each tool wants tighter control:

  - AINews   → always male regardless of topic
  - NewsEditor → male by default, female only when the topic is women /
    children focused (làm đẹp, mẹ bầu, trẻ em, …)

This module centralises that override so the policy reads from
``ChannelConfig.voice_gender_policy`` and the same keyword classifier can
be reused by any future tool.
"""
from __future__ import annotations

import logging

from .content_types import DEFAULT_VOICE_NAME, DEFAULT_VOICE_NAME_MALE
from .scene_models import ScenePlan

logger = logging.getLogger("briefa.planner.voice_policy")

# Topics that read better with a female voice. Match is substring + case-
# insensitive against the plan title + first scene's voice script — short
# but reliable. Add to this set when an obvious topic ends up on the wrong
# voice in practice.
FEMALE_TOPIC_KEYWORDS: frozenset[str] = frozenset({
    # Beauty / fashion / women
    "làm đẹp", "trang điểm", "mỹ phẩm", "skincare", "chăm sóc da",
    "thời trang", "mặc đẹp", "nail", "móng", "tóc",
    "phụ nữ", "chị em", "phái đẹp", "mỹ nhân", "hoa hậu",
    # Pregnancy / mothers
    "mẹ bầu", "thai sản", "sinh con", "sinh nở", "mang thai",
    "ở cữ", "sau sinh", "cho con bú",
    # Children
    "trẻ em", "em bé", "trẻ nhỏ", "trẻ sơ sinh", "bé yêu", "bé sơ sinh",
    "nuôi dạy", "dinh dưỡng cho bé", "ăn dặm",
    "giáo dục sớm", "mầm non", "mẫu giáo",
    # Home / cooking
    "công thức nấu", "nấu ăn", "bếp núc", "món ngon", "nội trợ",
})

VALID_POLICIES = ("channel_default", "always_male", "always_female", "auto_by_topic")


def detect_female_topic(text: str) -> bool:
    """Return True when ``text`` mentions a women / children keyword."""
    if not text:
        return False
    lower = text.lower()
    return any(kw in lower for kw in FEMALE_TOPIC_KEYWORDS)


def apply_voice_gender_policy(plan: ScenePlan, policy: str) -> str:
    """Override ``plan.voice_name`` according to a channel-level policy.

    Args:
        plan:    The validated :class:`ScenePlan` whose ``voice_name`` will
                 be set in place.
        policy:  One of :data:`VALID_POLICIES`. Unknown values fall back to
                 ``"channel_default"`` (no-op) with a warning so a typo in a
                 channel ``.env`` never crashes a render.

    Returns:
        The policy that was effectively applied (useful for logging).
    """
    if policy not in VALID_POLICIES:
        logger.warning(
            "unknown voice_gender_policy %r, keeping planner choice", policy,
        )
        return "channel_default"

    if policy == "always_male":
        plan.voice_name = DEFAULT_VOICE_NAME_MALE
        logger.info("voice policy: always_male -> %s", plan.voice_name)
    elif policy == "always_female":
        plan.voice_name = DEFAULT_VOICE_NAME
        logger.info("voice policy: always_female -> %s", plan.voice_name)
    elif policy == "auto_by_topic":
        sample_text = plan.title or ""
        if plan.scenes:
            sample_text = f"{sample_text} {plan.scenes[0].voice_script or ''}"
        if detect_female_topic(sample_text):
            plan.voice_name = DEFAULT_VOICE_NAME
            logger.info(
                "voice policy: auto_by_topic -> female (matched keyword in %r)",
                sample_text[:80],
            )
        else:
            plan.voice_name = DEFAULT_VOICE_NAME_MALE
            logger.info("voice policy: auto_by_topic -> male (no female keyword)")
    # "channel_default" → no-op, leave whatever planner set.
    return policy


__all__ = [
    "FEMALE_TOPIC_KEYWORDS",
    "VALID_POLICIES",
    "detect_female_topic",
    "apply_voice_gender_policy",
]
