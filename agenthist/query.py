"""Searching the index.

A Query is built (and validated) once from CLI flags or TUI fields, then turned into SQL.
Message-level filters (text, regex, time window, role) must all hold for the same message;
session-level filters (directory, source, subagents) apply to the conversation as a whole.
"""
from __future__ import annotations

import datetime as dt
import difflib
import os
import re
import shlex
import shutil
import sqlite3
from dataclasses import dataclass, field

from .sources import SOURCES
from .store import SESSION_FROM, SESSION_KEYS, SESSION_SELECT
from .util import HistError, iso, like_escape

ROLES = ("user", "assistant", "tool")
WHEN_HINT = ("use an ISO date/time (2026-10-01, 2026-10-01T14:00, 2026-10-01T14:00Z) "
             "or relative 90m, 12h, 3d, 2w, today, yesterday")


@dataclass
class Query:
    text: str | None = None        # case-insensitive substring
    regex: str | None = None       # Python regex (rg-compatible for common patterns)
    dir: str | None = None         # absolute path; matches that directory and everything below it
    since: str | None = None       # UTC ISO, inclusive
    until: str | None = None       # UTC ISO, exclusive
    sources: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    include_tools: bool = False
    include_subagents: bool = False
    missing_summary: bool = False  # only sessions without an annotation summary
    limit: int = 50

    @classmethod
    def from_args(cls, args: dict) -> "Query":
        """Build from argparse-style values, raising HistError with a hint for anything invalid."""
        q = cls(
            text=args.get("query") or None,
            regex=args.get("regex") or None,
            dir=normalize_dir(args.get("dir")),
            since=parse_when(args.get("since"), flag="--since"),
            until=parse_when(args.get("until"), flag="--until", end_of_day=True),
            sources=parse_sources(args.get("source")),
            roles=list(args.get("role") or []),
            include_tools=bool(args.get("tools")),
            include_subagents=bool(args.get("subagents")),
            missing_summary=bool(args.get("missing_summary")),
            limit=int(args["limit"]) if args.get("limit") is not None else 50,
        )
        if q.regex:
            try:
                re.compile(q.regex)
            except re.error as e:
                raise HistError(f"invalid --regex {q.regex!r}: {e}",
                                "Python re syntax (rg-compatible for common patterns); use -q for a literal substring")
        if q.limit < 1:
            raise HistError("--limit must be >= 1")
        if q.since and q.until and q.since >= q.until:
            raise HistError(f"--since ({args['since']}) is not before --until ({args['until']})",
                            "the window is [since, until)")
        return q

    @property
    def filters_messages(self) -> bool:
        return bool(self.text or self.regex or self.since or self.until or self.roles)

    def message_conditions(self) -> tuple[list[str], list]:
        """SQL conditions on `m` (messages) and their parameters."""
        conditions, params = [], []
        if self.roles:
            conditions.append(f"m.role in ({placeholders(self.roles)})")
            params += self.roles
        elif not self.include_tools:
            conditions.append("m.role in ('user', 'assistant')")
        if self.text:
            conditions.append("m.text like ? escape '\\'")
            params.append(f"%{like_escape(self.text)}%")
        if self.regex:
            conditions.append("m.text regexp ?")
            params.append(self.regex)
        if self.since:
            conditions.append("m.ts >= ?")
            params.append(self.since)
        if self.until:
            conditions.append("m.ts < ?")
            params.append(self.until)
        return conditions, params

    def session_conditions(self) -> tuple[list[str], list]:
        """SQL conditions on `s` (sessions) / `a` (annotations) and their parameters."""
        conditions, params = [], []
        if self.dir:
            conditions.append("(s.cwd = ? or s.cwd like ? escape '\\')")
            params += [self.dir, like_escape(self.dir) + "/%"]
        if self.sources:
            conditions.append(f"s.source in ({placeholders(self.sources)})")
            params += self.sources
        if not self.include_subagents:
            conditions.append("s.parent is null")
        if self.missing_summary:
            conditions.append("a.summary is null")
        return conditions, params

    def hit_pattern(self) -> re.Pattern | None:
        """What to highlight in matching messages."""
        if self.regex:
            return re.compile(self.regex)
        if self.text:
            return re.compile(re.escape(self.text), re.I)
        return None


# ---------------------------------------------------------------- argument parsing

def parse_when(value: str | None, flag: str = "--since", end_of_day: bool = False) -> str | None:
    """'2026-10-01', '2026-10-01T14:00', '3d', '12h', 'today', … → UTC ISO.
    Bare dates and today/yesterday are local midnight; with end_of_day they mean the end of that day."""
    if not value:
        return None
    text = value.strip().lower()
    now = dt.datetime.now().astimezone()
    try:
        relative = re.fullmatch(r"(\d+)\s*([mhdw])", text)
        if relative:
            amount, unit = int(relative.group(1)), relative.group(2)
            unit_name = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}[unit]
            moment = now - dt.timedelta(**{unit_name: amount})
        elif text in ("today", "yesterday"):
            moment = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if text == "yesterday":
                moment -= dt.timedelta(days=1)
            if end_of_day:
                moment += dt.timedelta(days=1)
        else:
            moment = dt.datetime.fromisoformat(text.upper().replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.astimezone()  # no zone given: local time
            if end_of_day and len(text) == 10:
                moment += dt.timedelta(days=1)  # --until 2026-10-01 includes all of Oct 1
    except (ValueError, OverflowError):
        raise HistError(f"invalid {flag} value {value!r}", WHEN_HINT)
    return iso(moment.isoformat())


def parse_sources(values: list[str] | None) -> list[str]:
    """['claude,codex', 'cursor'] → ['claude', 'codex', 'cursor'], rejecting unknown names."""
    names = [name.strip() for value in values or [] for name in value.split(",") if name.strip()]
    unknown = [name for name in names if name not in SOURCES]
    if unknown:
        raise HistError(f"unknown source {', '.join(unknown)}",
                        f"valid: {', '.join(SOURCES)} (repeat -s or comma-separate)")
    return names


def normalize_dir(path: str | None) -> str | None:
    if not path:
        return None
    return os.path.abspath(os.path.expanduser(path)).rstrip("/") or "/"


def placeholders(values: list) -> str:
    return ",".join("?" * len(values))


# ---------------------------------------------------------------- queries

def search(db: sqlite3.Connection, q: Query) -> list[dict]:
    """Matching sessions, most recent matching activity first. Each has a few matching snippets in hits_preview."""
    session_where, session_params = q.session_conditions()
    message_where, message_params = q.message_conditions()

    if q.filters_messages:
        rows = db.execute(f"""
            select {SESSION_SELECT}, count(*) as hits, max(m.ts) as last_hit
            from messages m
            join sessions s on s.id = m.session
            left join annotations a on a.id = s.id
            where {and_all(session_where + message_where)}
            group by s.id order by last_hit desc limit ?""",
            session_params + message_params + [q.limit]).fetchall()
    else:
        rows = db.execute(f"""
            select {SESSION_SELECT}, 0 as hits, s.updated as last_hit
            from {SESSION_FROM}
            where {and_all(session_where)}
            order by s.updated desc limit ?""",
            session_params + [q.limit]).fetchall()

    results = []
    for row in rows:
        session = dict(zip(SESSION_KEYS + ["hits", "last_hit"], row))
        session["resume"] = resume_command(session)
        if q.filters_messages:
            session["hits_preview"] = matching_snippets(db, session["id"], q, message_where, message_params)
        results.append(session)
    return results


def matching_snippets(db, session_id: str, q: Query, conditions: list[str], params: list, limit: int = 5) -> list[dict]:
    rows = db.execute(f"""
        select m.seq, m.ts, m.role, m.text from messages m
        where m.session = ? and {and_all(conditions)}
        order by m.seq limit ?""", [session_id] + params + [limit])
    return [{"seq": seq, "ts": ts, "role": role, "snippet": snippet(text, q)} for seq, ts, role, text in rows]


def snippet(text: str, q: Query, width: int = 220) -> str:
    """A one-line excerpt of text, starting a little before the first match."""
    text = re.sub(r"\s+", " ", text)
    pattern = q.hit_pattern()
    match = pattern.search(text) if pattern else None
    if not match:
        return text[:width]
    start = max(0, match.start() - width // 3)
    return ("…" if start else "") + text[start:start + width]


def and_all(conditions: list[str]) -> str:
    return " and ".join(conditions) or "1"


def resume_argv(session: dict) -> tuple[list[str], str | None] | None:
    """The command (and working directory to run it in) that reopens the conversation in its client."""
    source, native_id, cwd = session["source"], session["native_id"], session["cwd"]
    if cwd and not os.path.isdir(cwd):
        cwd = None
    if source == "claude":
        return ["claude", "--resume", native_id], cwd
    if source == "codex":
        return ["codex", "resume", native_id], cwd
    if source == "cursor":
        agent = shutil.which("cursor-agent")
        if agent:
            return [agent, "--resume", native_id], cwd
        if cwd:  # no CLI agent: open the workspace in the IDE, where the chat lives
            return (["cursor", cwd] if shutil.which("cursor") else ["open", "-a", "Cursor", cwd]), None
    return None


def resume_command(session: dict) -> str | None:
    """A shell command that reopens the conversation in its client."""
    resume = resume_argv(session)
    if not resume:
        return None
    argv, cwd = resume
    command = shlex.join(argv)
    return f"cd {shlex.quote(cwd)} && {command}" if cwd else command


def dir_warning(db: sqlite3.Connection, q: Query) -> str | None:
    """If --dir matches nothing, say so and suggest the closest directories that do have sessions."""
    if not q.dir:
        return None
    count = db.execute("select count(*) from sessions where cwd = ? or cwd like ? escape '\\'",
                       (q.dir, like_escape(q.dir) + "/%")).fetchone()[0]
    if count:
        return None
    known = [row[0] for row in db.execute("select distinct cwd from sessions where cwd is not null")]
    closest = difflib.get_close_matches(q.dir, known, n=5, cutoff=0.4)
    suggestion = f"; closest: {', '.join(closest)}" if closest else "; see `hist dirs`"
    return f"no sessions have a working directory under {q.dir}{suggestion}"


def list_dirs(db: sqlite3.Connection, sources: list[str], since: str | None, limit: int) -> list[dict]:
    """Working directories with sessions, most recently active first."""
    conditions, params = ["s.cwd is not null", "s.parent is null"], []
    if sources:
        conditions.append(f"s.source in ({placeholders(sources)})")
        params += sources
    if since:
        conditions.append("s.updated >= ?")
        params.append(since)
    rows = db.execute(f"""
        select s.cwd, count(*), max(s.updated), group_concat(distinct s.source)
        from sessions s where {and_all(conditions)}
        group by s.cwd order by max(s.updated) desc limit ?""", params + [limit])
    return [dict(zip(["cwd", "sessions", "last_active", "sources"], row)) for row in rows]


def source_stats(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute("""
        select source, count(*), min(started), max(updated), sum(n_msgs)
        from sessions group by source""")
    return [dict(zip(["source", "sessions", "first", "last", "messages"], row)) for row in rows]
