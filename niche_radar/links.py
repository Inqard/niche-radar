"""Parses YouTube links pasted by hand (related videos, home page) into (kind, value)."""
from __future__ import annotations

import re

_VIDEO_ID = r"[A-Za-z0-9_-]{11}"
_CHANNEL_ID = r"UC[A-Za-z0-9_-]{22}"
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("video", re.compile(rf"[?&]v=({_VIDEO_ID})(?![A-Za-z0-9_-])")),
    ("video", re.compile(rf"youtu\.be/({_VIDEO_ID})(?![A-Za-z0-9_-])")),
    ("video", re.compile(rf"/(?:shorts|live|embed)/({_VIDEO_ID})(?![A-Za-z0-9_-])")),
    ("channel", re.compile(rf"/channel/({_CHANNEL_ID})(?![A-Za-z0-9_-])")),
    ("handle", re.compile(r"(?:^|/)@([A-Za-z0-9._-]{3,30})(?:[/?#]|$)")),
    ("channel", re.compile(rf"^({_CHANNEL_ID})$")),
    ("video", re.compile(rf"^({_VIDEO_ID})$")),
]


def parse_link(text: str) -> tuple[str, str] | None:
    """('video', id) | ('channel', UC…) | ('handle', name without @) | None."""
    text = text.strip()
    for kind, pattern in _PATTERNS:
        m = pattern.search(text)
        if m:
            return kind, m.group(1)
    return None


def parse_many(lines: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """(recognised links without duplicates, unrecognised non-empty lines)."""
    found: list[tuple[str, str]] = []
    rejected: list[str] = []
    for line in lines:
        for token in line.split():
            parsed = parse_link(token)
            if parsed and parsed not in found:
                found.append(parsed)
            elif not parsed:
                rejected.append(token)
    return found, rejected
