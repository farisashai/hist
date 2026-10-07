"""Small shared helpers: errors, timestamps, text cleanup, display formatting."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys

HOME = os.path.expanduser("~")


class HistError(Exception):
    """A user-facing error. code: 2 = invalid arguments, 3 = session not found/ambiguous."""

    def __init__(self, msg: str, hint: str | None = None, code: int = 2):
        super().__init__(msg)
        self.hint, self.code = hint, code


def note(msg: str) -> None:
    print(f"hist: {msg}", file=sys.stderr)


# ---------------------------------------------------------------- time

def iso(ts) -> str | None:
    """Normalize epoch-ms / epoch-s / ISO strings to UTC 'YYYY-MM-DDTHH:MM:SS.mmmZ'."""
    if ts is None or ts == "":
        return None
    if isinstance(ts, (int, float)):
        if ts > 1e11:
            ts = ts / 1000.0
        return _fmt(dt.datetime.fromtimestamp(ts, dt.timezone.utc))
    s = str(ts)
    if s.isdigit():
        return iso(int(s))
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return s
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return _fmt(d)


def _fmt(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def local(ts: str | None) -> str:
    """UTC ISO timestamp → 'YYYY-MM-DD HH:MM' in local time, for display."""
    if not ts:
        return "?"
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts


def short_path(p: str | None) -> str:
    return (p or "?").replace(HOME, "~")


# ---------------------------------------------------------------- text

def cap(s, n: int) -> str:
    """Truncate to n chars with a marker; non-strings are JSON-encoded first."""
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + f" …[+{len(s) - n} chars]"


def like_escape(s: str) -> str:
    """Escape % _ and \\ for a SQL LIKE … ESCAPE '\\' pattern."""
    return re.sub(r"([%_\\])", r"\\\1", s)


# User messages that clients inject (environment blocks, slash-command echoes, reminders) rather than the human typing.
INJECTED = re.compile(
    r"^\s*(<environment_context>|<user_instructions>|<permissions|<model_switch>|# AGENTS\.md|<INSTRUCTIONS>|<turn_aborted>"
    r"|<local-command|<command-name>|<system-reminder>|<task-notification>|Caveat: |\[Request interrupted)")
_LEADING_TAG = re.compile(r"^\s*<([\w:-]+)[^>]*>.*?</\1>\s*", re.S)


def human_text(t: str) -> str:
    """The part of a user message the human typed: drop injected <tag>…</tag> preambles and meta lines."""
    prev = None
    while prev != t:
        prev, t = t, _LEADING_TAG.sub("", t, count=1)
    return "" if INJECTED.match(t) else t.strip()
