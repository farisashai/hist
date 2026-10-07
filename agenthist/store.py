"""The SQLite index: schema, writing parsed sessions, and reading sessions/messages back."""
from __future__ import annotations

import os
import re
import sqlite3
import time
from dataclasses import dataclass, field

from .util import HOME, HistError, human_text, iso

DB_PATH = os.environ.get("HIST_DB", os.path.join(HOME, ".cache", "agent-history", "index.db"))

# Bump when the derived tables change shape; they are dropped and rebuilt. Annotations are never dropped.
SCHEMA_VERSION = "4"

SCHEMA = """
create table if not exists files(path text primary key, mtime real, size int);
create table if not exists sessions(
    id text primary key, source text, native_id text, path text, cwd text, title text,
    started text, updated text, branch text, n_msgs int, parent text,
    native_summary text, first_prompt text, last_prompt text);
create table if not exists messages(session text, seq int, ts text, role text, text text);
create table if not exists annotations(
    id text primary key, title text, summary text, tags text, by text, updated text);
create index if not exists m_session on messages(session, seq);
create index if not exists m_ts on messages(ts);
create index if not exists s_cwd on sessions(cwd);
"""

# Sessions that `hist summarize` itself creates by running `claude -p` etc. are kept out of the index.
SUMMARIZER_MARKER = "You are labeling a past AI coding-assistant session"


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("pragma journal_mode=wal")
    db.execute("pragma synchronous=normal")
    db.execute("create table if not exists meta(k text primary key, v text)")

    row = db.execute("select v from meta where k='schema'").fetchone()
    if not row or row[0] != SCHEMA_VERSION:
        db.executescript("drop table if exists files; drop table if exists sessions; drop table if exists messages;")
        db.execute("insert or replace into meta values('schema', ?)", (SCHEMA_VERSION,))

    db.executescript(SCHEMA)
    db.commit()
    db.create_function("regexp", 2, _sql_regexp, deterministic=True)
    return db


_compiled: dict[str, re.Pattern] = {}


def _sql_regexp(pattern: str, text: str | None) -> int:
    """Backs `text REGEXP pattern` in SQL."""
    if text is None:
        return 0
    if pattern not in _compiled:
        _compiled[pattern] = re.compile(pattern)
    return 1 if _compiled[pattern].search(text) else 0


# ---------------------------------------------------------------- writing

@dataclass
class Session:
    """One conversation as parsed from a client, before it is written to the index."""
    id: str             # "<source>:<native_id>", unique across clients
    source: str         # claude | claude-desktop | codex | cursor
    native_id: str      # the client's own id (what `claude --resume` / `codex resume` take)
    path: str           # transcript file (or Cursor's state DB)
    cwd: str | None = None
    title: str | None = None
    branch: str | None = None
    parent: str | None = None          # set for subagent sessions
    native_summary: str | None = None
    messages: list = field(default_factory=list)  # [(iso_ts, role, text)], role: user | assistant | tool

    def add(self, ts, role: str, text: str) -> None:
        if text and text.strip():
            self.messages.append((iso(ts), role, text))

    def is_summarizer_run(self) -> bool:
        return any(role == "user" and text.startswith(SUMMARIZER_MARKER) for _, role, text in self.messages[:3])


def save_session(db: sqlite3.Connection, s: Session) -> None:
    """Replace the session's rows. Empty sessions and our own summarizer runs are dropped."""
    db.execute("delete from messages where session=?", (s.id,))
    db.execute("delete from sessions where id=?", (s.id,))
    if not s.messages or s.is_summarizer_run():
        return

    timestamps = [ts for ts, _, _ in s.messages if ts]
    prompts = [p for p in (human_text(text) for _, role, text in s.messages if role == "user") if p]
    substantive = [p for p in prompts if len(p) >= 20]  # skip "continue", "resume", … for the opening prompt
    first_prompt = (substantive or prompts or [None])[0]
    last_prompt = prompts[-1] if prompts else None

    db.execute("insert into sessions values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        s.id, s.source, s.native_id, s.path, s.cwd, s.title,
        min(timestamps, default=None), max(timestamps, default=None),
        s.branch, len(s.messages), s.parent,
        s.native_summary, _one_line(first_prompt), _one_line(last_prompt)))
    db.executemany("insert into messages values(?,?,?,?,?)",
                   [(s.id, seq, ts, role, text) for seq, (ts, role, text) in enumerate(s.messages)])


def _one_line(text: str | None, limit: int = 1000) -> str | None:
    return re.sub(r"\s+", " ", text).strip()[:limit] if text else None


def delete_session(db: sqlite3.Connection, session_id: str) -> None:
    db.execute("delete from messages where session=?", (session_id,))
    db.execute("delete from sessions where id=?", (session_id,))


# ---------------------------------------------------------------- change tracking

def is_unchanged(db: sqlite3.Connection, key: str, mtime: float, size: int = 0) -> bool:
    row = db.execute("select mtime, size from files where path=?", (key,)).fetchone()
    return bool(row) and row[0] == mtime and row[1] == size


def remember(db: sqlite3.Connection, key: str, mtime: float, size: int = 0) -> None:
    db.execute("insert or replace into files values(?,?,?)", (key, mtime, size))


def forget(db: sqlite3.Connection, key: str) -> None:
    db.execute("delete from files where path=?", (key,))


# ---------------------------------------------------------------- reading

# Each output field with the SQL that produces it. Title/summary precedence:
# your annotation > what the client stores natively > the first real prompt.
SESSION_FIELDS = [
    ("id", "s.id"),
    ("source", "s.source"),
    ("native_id", "s.native_id"),
    ("path", "s.path"),
    ("cwd", "s.cwd"),
    ("title", "coalesce(a.title, s.title, substr(s.first_prompt, 1, 80))"),
    ("summary", "coalesce(a.summary, s.native_summary, substr(s.first_prompt, 1, 400))"),
    ("summary_source", """case when a.summary is not null then 'annotation:' || coalesce(a.by, '?')
                               when s.native_summary is not null then 'native'
                               else 'first_prompt' end"""),
    ("first_prompt", "s.first_prompt"),
    ("last_prompt", "s.last_prompt"),
    ("tags", "a.tags"),
    ("started", "s.started"),
    ("updated", "s.updated"),
    ("branch", "s.branch"),
    ("n_msgs", "s.n_msgs"),
    ("parent", "s.parent"),
]
SESSION_KEYS = [key for key, _ in SESSION_FIELDS]
SESSION_SELECT = ", ".join(sql for _, sql in SESSION_FIELDS)
SESSION_FROM = "sessions s left join annotations a on a.id = s.id"


def get_session(db: sqlite3.Connection, session_id: str) -> dict | None:
    row = db.execute(f"select {SESSION_SELECT} from {SESSION_FROM} where s.id=?", (session_id,)).fetchone()
    return dict(zip(SESSION_KEYS, row)) if row else None


def get_messages(db: sqlite3.Connection, session_id: str, include_tools: bool = True) -> list[tuple]:
    """[(seq, ts, role, text)] in conversation order."""
    sql = "select seq, ts, role, text from messages where session=?"
    if not include_tools:
        sql += " and role != 'tool'"
    return db.execute(sql + " order by seq", (session_id,)).fetchall()


def resolve_session(db: sqlite3.Connection, ref: str) -> str:
    """Find a session by exact id/native id, else a unique prefix, else a unique substring."""
    ref = ref.strip()
    row = db.execute("select id from sessions where id=? or native_id=?", (ref, ref)).fetchone()
    if row:
        return row[0]

    for pattern in (f"{ref}%", f"%{ref}%"):
        candidates = db.execute("""
            select id, coalesce(title, substr(first_prompt, 1, 60)) from sessions
            where native_id like ? or id like ? order by updated desc limit 6""", (pattern, pattern)).fetchall()
        if len(candidates) == 1:
            return candidates[0][0]
        if candidates:
            listing = "; ".join(f"{sid} ({title})" for sid, title in candidates[:5])
            raise HistError(f"session {ref!r} is ambiguous ({len(candidates)}+ matches)",
                            f"use a longer prefix: {listing}", code=3)

    raise HistError(f"no session matching {ref!r}", "find ids with `hist search ... --json` (field: id or native_id)", code=3)


# ---------------------------------------------------------------- annotations

def annotate(db: sqlite3.Connection, session_id: str, title=None, summary=None, tags=None, by="user") -> None:
    """Set any of title/summary/tags, keeping existing values for the ones passed as None."""
    old = db.execute("select title, summary, tags from annotations where id=?", (session_id,)).fetchone()
    old_title, old_summary, old_tags = old or (None, None, None)
    db.execute("insert or replace into annotations values(?,?,?,?,?,?)", (
        session_id,
        title if title is not None else old_title,
        summary if summary is not None else old_summary,
        tags if tags is not None else old_tags,
        by,
        iso(time.time())))
    db.commit()


def clear_annotation(db: sqlite3.Connection, session_id: str) -> None:
    db.execute("delete from annotations where id=?", (session_id,))
    db.commit()
