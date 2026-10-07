"""Finds every client's sessions on disk and keeps the index in sync with them.

Each client module exposes `index(db, paths, full) -> int` (number of sessions re-parsed).
File-based clients (Claude, Codex) share `index_files`; Cursor reads its own SQLite database.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from typing import Callable, Iterable

from .. import store
from ..util import HOME

SOURCES = ("claude", "claude-desktop", "codex", "cursor")

# Cap on stored text, so one pasted log or tool dump can't dominate the index.
TEXT_LIMIT = 20_000
TOOL_LIMIT = 2_000


@dataclass
class Paths:
    """Where each client keeps its data (macOS layout)."""
    claude_projects: str
    claude_desktop: str
    codex_home: str
    cursor_state_db: str
    cursor_workspaces: str
    cursor_projects: str

    @classmethod
    def for_home(cls, home: str = HOME) -> "Paths":
        app_support = os.path.join(home, "Library", "Application Support")
        return cls(
            claude_projects=os.path.join(home, ".claude", "projects"),
            claude_desktop=os.path.join(app_support, "Claude"),
            codex_home=os.path.join(home, ".codex"),
            cursor_state_db=os.path.join(app_support, "Cursor", "User", "globalStorage", "state.vscdb"),
            cursor_workspaces=os.path.join(app_support, "Cursor", "User", "workspaceStorage"),
            cursor_projects=os.path.join(home, ".cursor", "projects"),
        )


def index_all(db: sqlite3.Connection, full: bool = False, quiet: bool = False, paths: Paths | None = None) -> None:
    """Bring the index up to date. Incremental unless full=True."""
    from . import claude, codex, cursor  # local import: the client modules import helpers from this package

    paths = paths or Paths.for_home()
    started = time.time()
    for name, module in (("claude", claude), ("codex", codex), ("cursor", cursor)):
        updated = module.index(db, paths, full)
        db.commit()
        if updated and not quiet:
            print(f"{name}: {updated} sessions updated", file=sys.stderr)
    remove_deleted_files(db)
    db.commit()
    if not quiet:
        print(f"index ready in {time.time() - started:.1f}s → {store.DB_PATH}", file=sys.stderr)


def index_files(db: sqlite3.Connection, files: Iterable[str], parse: Callable[[str], store.Session], full: bool) -> int:
    """Re-parse the files whose size or mtime changed since the last run."""
    updated = 0
    for path in files:
        try:
            stat = os.stat(path)
        except OSError:
            continue
        if not full and store.is_unchanged(db, path, stat.st_mtime, stat.st_size):
            continue
        try:
            store.save_session(db, parse(path))
        except Exception as e:  # one unreadable transcript must not break the whole index
            print(f"hist: skipped {path}: {e}", file=sys.stderr)
        store.remember(db, path, stat.st_mtime, stat.st_size)
        updated += 1
    return updated


def remove_deleted_files(db: sqlite3.Connection) -> None:
    """Drop sessions whose transcript file no longer exists."""
    tracked = [row[0] for row in db.execute("select path from files where path like '/%'")]
    for path in tracked:
        if not os.path.exists(path):
            for (session_id,) in db.execute("select id from sessions where path=?", (path,)).fetchall():
                store.delete_session(db, session_id)
            store.forget(db, path)
