"""User settings: ~/.config/agent-history/config.toml (or $HIST_CONFIG).

    [display]
    date_format = "%b %-d, %Y %-I:%M%p"   # strftime, in your local time zone and locale
    relative_within = "1d"               # newer times read "32 minutes ago"; "" = always date_format

    [summarize]
    command = "claude -p --model sonnet"  # what `hist summarize` runs; "" = the default (haiku)

Python 3.11+ reads the file with tomllib; older versions use a small reader for the subset hist needs
(tables, and string/number/boolean values). A bad file never stops hist: it warns and uses the defaults.
"""
from __future__ import annotations

import datetime as dt
import locale
import os
import re
from functools import lru_cache

from .util import HOME, note

CONFIG_PATH = os.environ.get("HIST_CONFIG", os.path.join(HOME, ".config", "agent-history", "config.toml"))

DEFAULTS = {
    "display": {
        "date_format": "%Y-%m-%d %H:%M",
        "relative_within": "",
    },
    "summarize": {
        "command": "",
    },
}

TEMPLATE = """\
# hist settings. Run `hist config` to see the values in effect.

[display]
# How times are shown (strftime codes, in your local time zone and locale).
#   "%Y-%m-%d %H:%M"        2026-11-04 11:23   (default)
#   "%b %-d, %Y %-I:%M%p"   Nov 4, 2026 11:23AM
date_format = "%Y-%m-%d %H:%M"

# Show times newer than this as "32 minutes ago", "5 hours ago": 90m, 12h, 1d, 2w; "" = never.
relative_within = ""

[summarize]
# The command `hist summarize` runs: it reads the prompt on stdin and prints JSON. "" = claude -p --model haiku.
# $HIST_SUMMARIZER and --cmd-line override it. Examples: "claude -p --model sonnet", "codex exec",
# or a local model such as "ollama run llama3.2" to keep transcripts offline.
command = ""
"""

DURATION = re.compile(r"^\s*(\d+)\s*([mhdw])\s*$")
SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


@lru_cache(maxsize=None)
def settings() -> dict:
    """DEFAULTS overlaid with the config file's values; unknown or mistyped keys are reported and ignored."""
    merged = {table: dict(values) for table, values in DEFAULTS.items()}
    if not os.path.exists(CONFIG_PATH):
        return merged
    try:
        with open(CONFIG_PATH, "rb") as f:
            loaded = parse_toml(f.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as e:
        note(f"ignoring {CONFIG_PATH}: {e}")
        return merged

    for table, values in loaded.items():
        if table not in DEFAULTS or not isinstance(values, dict):
            note(f"{CONFIG_PATH}: unknown setting [{table}]")
            continue
        for key, value in values.items():
            default = DEFAULTS[table].get(key)
            if default is None:
                note(f"{CONFIG_PATH}: unknown setting {table}.{key}")
            elif type(value) is not type(default):
                note(f"{CONFIG_PATH}: {table}.{key} should be a {type(default).__name__}")
            else:
                merged[table][key] = value

    if merged["display"]["relative_within"] and duration_seconds(merged["display"]["relative_within"]) is None:
        note(f"{CONFIG_PATH}: display.relative_within should look like 90m, 12h, 1d or 2w")
        merged["display"]["relative_within"] = ""
    return merged


def duration_seconds(text: str) -> int | None:
    m = DURATION.match(text)
    return int(m.group(1)) * SECONDS[m.group(2)] if m else None


# ---------------------------------------------------------------- displaying times

@lru_cache(maxsize=None)
def _display() -> tuple[str, int]:
    """(strftime format, relative window in seconds). Month and AM/PM names follow your locale."""
    try:
        locale.setlocale(locale.LC_TIME, "")
    except locale.Error:
        pass
    display = settings()["display"]
    return display["date_format"], duration_seconds(display["relative_within"] or "0m") or 0


def format_time(when: dt.datetime, now: dt.datetime | None = None) -> str:
    """A local time per the display settings: '32 minutes ago' inside the relative window, else date_format."""
    date_format, relative_within = _display()
    now = now or dt.datetime.now(dt.timezone.utc)
    age = (now - when).total_seconds()
    if 0 <= age < relative_within:
        return relative(age)
    return when.astimezone().strftime(date_format)


def relative(age: float) -> str:
    for unit, seconds in (("week", 7 * 86400), ("day", 86400), ("hour", 3600), ("minute", 60)):
        if age >= seconds:
            count = int(age // seconds)
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    return "just now"


@lru_cache(maxsize=None)
def time_width() -> int:
    """The widest a displayed time can be, so columns of times line up."""
    date_format, relative_within = _display()
    samples = [dt.datetime(2026, month, 28, hour, 59) for month in range(1, 13) for hour in (0, 11, 12, 23)]
    widest = max(len(sample.strftime(date_format)) for sample in samples)
    if relative_within:
        widest = max(widest, max(len(relative(age)) for age in (59 * 60, 23 * 3600, 6 * 86400, 51 * 7 * 86400)
                                if age < relative_within))
    return widest


# ---------------------------------------------------------------- TOML

def parse_toml(text: str) -> dict:
    try:
        import tomllib  # Python 3.11+
        return tomllib.loads(text)
    except ImportError:
        return parse_simple_toml(text)


TABLE = re.compile(r"^\[\s*([A-Za-z0-9_-]+)\s*\]$")
KEY_VALUE = re.compile(r"^([A-Za-z0-9_-]+)\s*=\s*(.+)$")


def parse_simple_toml(text: str) -> dict:
    """The subset of TOML hist's settings use: [tables], and key = "string" | 'literal' | number | true/false."""
    data = {}
    current = data
    for number, raw in enumerate(text.splitlines(), 1):
        line = strip_comment(raw).strip()
        if not line:
            continue
        m = TABLE.match(line)
        if m:
            current = data.setdefault(m.group(1), {})
            continue
        m = KEY_VALUE.match(line)
        if not m:
            raise ValueError(f"line {number}: expected `key = value` or `[table]`")
        current[m.group(1)] = parse_value(m.group(2).strip(), number)
    return data


def strip_comment(line: str) -> str:
    """Drop a trailing # comment that isn't inside a string."""
    quote, escaped = None, False
    for i, ch in enumerate(line):
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\" and quote == '"':
                escaped = True
            elif ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            return line[:i]
    return line


def parse_value(text: str, number: int):
    if len(text) >= 2 and text[0] == text[-1] == "'":
        return text[1:-1]
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return re.sub(r'\\(["\\])', r"\1", text[1:-1])
    if text in ("true", "false"):
        return text == "true"
    if re.fullmatch(r"[+-]?\d+", text):
        return int(text)
    raise ValueError(f"line {number}: unsupported value {text!r} (use a quoted string, number or true/false)")
