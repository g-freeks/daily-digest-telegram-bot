"""Splits a full digest into a chat-sized message plus a full-text attachment."""
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

TELEGRAM_MESSAGE_LIMIT = 4096
_SAFETY_MARGIN = 200  # room for the "full digest attached" footer


@dataclass
class Outcome:
    """What a run wants posted: the message, the archived full text (sent as
    an attachment if the message is too long) and whether the run failed."""
    message: str
    archive_path: Optional[Path] = None
    failed: bool = False


def extract_headlines(markdown: str, limit: int) -> str:
    lines = markdown.splitlines()
    start = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") and "headlines" in stripped.lower():
            start = i
            break

    if start is not None:
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if lines[j].strip().startswith("#"):
                end = j
                break
        section = "\n".join(lines[start:end]).strip()
    else:
        section = markdown.strip()

    if len(section) > limit:
        section = section[: limit - 1].rstrip() + "…"
    return section


def split_for_telegram(markdown: str) -> tuple[str, bool]:
    """Returns (message_text, needs_attachment)."""
    if len(markdown) <= TELEGRAM_MESSAGE_LIMIT:
        return markdown, False

    short = extract_headlines(markdown, TELEGRAM_MESSAGE_LIMIT - _SAFETY_MARGIN)
    message = f"{short}\n\n(Full digest attached as a file.)"
    return message, True
