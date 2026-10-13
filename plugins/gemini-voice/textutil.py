"""Text helpers: label matching, the reply-block marker, plain-text rendering, localized strings."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable, List, Optional

# Every block this plugin writes into a task object starts with MARKER; Gemini reads it as "Hermes".
# The marker is cosmetic: the plugin finds its own block by the exact text it recorded when writing,
# never by searching for the marker, so a marker in the owner's text or in a reply deletes nothing.
MARKER = "[Hermes]"
# First line of every document the cron output channel creates. It is checked again when a document
# is read as a task, so an output can never come back in as an owner task, even without the journal.
OUTPUT_MARKER = "[Hermes digest]"
_MARKER_FORGERY = re.compile(r"\[+\s*[*_`~]*\s*hermes(\s+digest)?\s*[*_`~]*\s*\]+", re.IGNORECASE)

STRINGS = {
    "en": {
        "ack": "got it, working on it ({time})",
        "reply": "reply ({time}):",
        "abandoned": "this task did not finish (restart or failure). Dictate it again if it is still needed",
        "failed": "the task failed ({reason}). Dictate it again if it is still needed",
        "done_silent": "done, no text reply ({time})",
        "daily_cap": "daily task limit reached ({limit}); this task was not run",
        "stale": "this task waited too long and was not run. Dictate it again if it is still needed",
        "truncated": "(reply shortened)",
    },
    "ru": {
        "ack": "принял, работаю ({time})",
        "reply": "ответ ({time}):",
        "abandoned": "поручение не завершено (перезапуск или сбой). Повторите, если ещё нужно",
        "failed": "не получилось выполнить ({reason}). Повторите, если ещё нужно",
        "done_silent": "готово, текстового ответа нет ({time})",
        "daily_cap": "дневной лимит поручений исчерпан ({limit}), это поручение не выполнялось",
        "stale": "поручение слишком долго ждало очереди и не выполнялось. Повторите, если ещё нужно",
        "truncated": "(ответ сокращён)",
    },
}

_MARKDOWN_SUBS = (
    (re.compile(r"```[A-Za-z0-9_-]*\n?"), ""),
    (re.compile(r"`([^`\n]+)`"), r"\1"),
    (re.compile(r"\*\*(.+?)\*\*", re.DOTALL), r"\1"),
    (re.compile(r"__(.+?)__", re.DOTALL), r"\1"),
    (re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", re.DOTALL), r"\1"),
    (re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE), ""),
    (re.compile(r"^\s{0,3}>\s?", re.MULTILINE), ""),
    (re.compile(r"^(\s*)[*+]\s+", re.MULTILINE), r"\1- "),
    (re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)"), r"\1"),
    (re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)"), r"\1 (\2)"),
    (re.compile(r"\n{3,}"), "\n\n"),
)


def strings(language: str) -> dict:
    """Localized status strings; unknown languages fall back to English."""
    return STRINGS.get((language or "en").lower(), STRINGS["en"])


def compile_labels(labels: Iterable[str]) -> List[re.Pattern]:
    """Case-insensitive patterns for the configured labels: a whole word, or a word prefix when the label
    ends with ``*`` (``Афон*`` matches ``Афоня``, ``Афоне``, ``Афоню``). Empty labels are dropped."""
    patterns = []
    for label in labels:
        word = str(label or "").strip()
        prefix = word.endswith("*")
        word = word.rstrip("*").strip()
        if word:
            tail = r"\w*" if prefix else r"(?!\w)"
            patterns.append(re.compile(r"(?<!\w)" + re.escape(word) + tail, re.IGNORECASE))
    return patterns


def has_label(title: Optional[str], patterns: List[re.Pattern]) -> bool:
    """True when the title contains any configured label as a separate word."""
    text = title or ""
    return any(p.search(text) for p in patterns)


def is_output(text: Optional[str]) -> bool:
    """True when a document's text carries the output-channel signature (its first non-empty line)."""
    for line in (text or "").split("\n"):
        if line.strip():
            return line.strip().startswith(OUTPUT_MARKER)
    return False


def remove_suffix_block(text: str, block_text: Optional[str]) -> str:
    """``text`` without the exact block this plugin last wrote, when that block is still there."""
    if not block_text:
        return text
    at = text.rfind(block_text)
    return text if at < 0 else (text[:at] + text[at + len(block_text):])


def block(header: str, body: str = "") -> str:
    """A status or reply block: the marker header line, then the body (if any)."""
    head = f"{MARKER} {header.strip()}"
    return head if not body.strip() else head + "\n" + body.strip()


def _unforge(text: str) -> str:
    previous = None
    while previous != text:  # repeat: removing one layer of markup can reveal another
        previous = text
        text = _MARKER_FORGERY.sub("Hermes", text)
    return text


def plain_text(content: Optional[str]) -> str:
    """Markdown rendered as plain text for a speech reader; our markers cannot be forged by the agent."""
    text = _unforge((content or "").replace("\r\n", "\n"))
    for pattern, repl in _MARKDOWN_SUBS:
        text = pattern.sub(repl, text)
    return _unforge(text).strip()


def truncate(text: str, limit: int, note: str) -> str:
    """Cut text to ``limit`` characters on a line or word boundary, appending ``note``."""
    if len(text) <= limit:
        return text
    room = max(0, limit - len(note) - 2)
    cut = text[:room]
    boundary = max(cut.rfind("\n"), cut.rfind(" "))
    if boundary > room // 2:
        cut = cut[:boundary]
    return cut.rstrip() + "\n" + note


def time_label(moment: datetime) -> str:
    """Short local time label used in status lines, e.g. ``14.10 09:58``."""
    return moment.strftime("%d.%m %H:%M")
