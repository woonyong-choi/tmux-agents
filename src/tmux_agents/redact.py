"""Scrub secrets out of pane text before it reaches the model."""

from __future__ import annotations

import re

_PATTERNS = [
    # GitHub, OpenAI, Anthropic, Slack, AWS, generic bearer/API keys
    re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(
        r"(?i)\b([A-Z0-9_]*(?:token|secret|api[_-]?key|password|passwd))"
        r"\s*[=:]\s*['\"]?[^\s'\"]{8,}"
    ),
]

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][A-Za-z0-9]")


def redact(text: str) -> str:
    for pattern in _PATTERNS:
        text = pattern.sub(_replace, text)
    return text


def _replace(match: re.Match[str]) -> str:
    groups = match.groups()
    if groups and groups[0]:
        return (
            f"{groups[0]}=[redacted]"
            if "=" in match.group(0) or ":" in match.group(0)
            else f"{groups[0]} [redacted]"
        )
    return "[redacted]"


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)
