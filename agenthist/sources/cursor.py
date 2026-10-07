"""Cursor: IDE agent/chat conversations (including synced background agents) and agent transcripts.

Conversations live in Cursor's global state DB (table cursorDiskKV):
  composerData:<composer id>            → {name, subtitle, workspaceIdentifier, fullConversationHeadersOnly, ...}
  bubbleId:<composer id>:<bubble id>    → one message: {type: 1 user | 2 assistant, text, createdAt, toolFormerData}
Agent transcripts (~/.cursor/projects/<path slug>/agent-transcripts/**/*.jsonl) cover the CLI agent and
conversations whose DB rows Cursor has already cleaned up.
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import re
import sqlite3
from urllib.parse import unquote

from .. import store
from ..store import Session
from ..util import cap, iso
from . import TEXT_LIMIT, TOOL_LIMIT, Paths

USER_BUBBLE = 1


def index(db, paths: Paths, full: bool) -> int:
    composer_ids, updated = set(), 0
    if os.path.exists(paths.cursor_state_db):
        composer_ids, updated = index_composers(db, paths, full)
    return updated + index_transcripts(db, paths, full, skip=composer_ids)


# ---------------------------------------------------------------- composers (state.vscdb)

def index_composers(db, paths: Paths, full: bool) -> tuple[set[str], int]:
    cursor_db = sqlite3.connect(f"file:{paths.cursor_state_db}?mode=ro", uri=True)
    cursor_db.text_factory = lambda b: b.decode("utf-8", "replace")
    workspace_dirs = load_workspace_dirs(paths.cursor_workspaces)
    workspace_of = load_composer_workspaces(cursor_db)

    seen, updated = set(), 0
    for key, value in cursor_db.execute("select key, value from cursorDiskKV where key like 'composerData:%'"):
        try:
            composer = json.loads(value)
        except ValueError:
            continue
        composer_id = composer.get("composerId") or key.split(":", 1)[1]
        seen.add(composer_id)

        # Composers have no file of their own; their lastUpdatedAt plays the role of an mtime.
        change_key = f"cursor-composer:{composer_id}"
        stamp = float(composer.get("lastUpdatedAt") or composer.get("createdAt") or 0)
        if not full and store.is_unchanged(db, change_key, stamp):
            continue

        session = parse_composer(cursor_db, composer, composer_id, paths.cursor_state_db)
        if not session.cwd:
            workspace_id = (composer.get("workspaceIdentifier") or {}).get("id") or workspace_of.get(composer_id)
            session.cwd = workspace_dirs.get(workspace_id)
        store.save_session(db, session)
        store.remember(db, change_key, stamp)
        updated += 1

    remove_deleted_composers(db, seen)
    return seen, updated


def parse_composer(cursor_db, composer: dict, composer_id: str, db_path: str) -> Session:
    session = Session(id=f"cursor:{composer_id}", source="cursor", native_id=composer_id, path=db_path)
    session.title = composer.get("name")
    subtitle = composer.get("subtitle")
    if subtitle and subtitle != "New chat":
        session.native_summary = subtitle  # e.g. "Edited bundle.go, values.yaml"
    workspace = composer.get("workspaceIdentifier") or {}
    session.cwd = (workspace.get("uri") or {}).get("fsPath")

    bubbles = load_bubbles(cursor_db, composer_id)
    headers = composer.get("fullConversationHeadersOnly") or []
    header_times = {h.get("bubbleId"): h.get("createdAt") for h in headers}
    order = [h.get("bubbleId") for h in headers] or sorted(bubbles, key=lambda b: bubbles[b].get("createdAt") or "")

    for bubble_id in order:
        bubble = bubbles.get(bubble_id)
        if not bubble:
            continue
        timestamp = bubble.get("createdAt") or header_times.get(bubble_id) or bubble.get("startedAtMs")
        for role, text in bubble_parts(bubble):
            session.add(timestamp, role, text)

    if not session.messages and composer.get("text"):  # an unsent draft
        session.add(composer.get("createdAt"), "user", composer["text"])
    return session


def load_bubbles(cursor_db, composer_id: str) -> dict[str, dict]:
    # A key range scan; ';' is the character right after ':' so this covers exactly this composer's bubbles.
    rows = cursor_db.execute("select key, value from cursorDiskKV where key >= ? and key < ?",
                             (f"bubbleId:{composer_id}:", f"bubbleId:{composer_id};"))
    bubbles = {}
    for key, value in rows:
        try:
            bubbles[key.rsplit(":", 1)[1]] = json.loads(value)
        except ValueError:
            pass
    return bubbles


def bubble_parts(bubble: dict) -> list[tuple[str, str]]:
    parts = []
    if bubble.get("text"):
        role = "user" if bubble.get("type") == USER_BUBBLE else "assistant"
        parts.append((role, cap(bubble["text"], TEXT_LIMIT)))
    tool = bubble.get("toolFormerData")
    if isinstance(tool, dict) and tool.get("name"):
        args = tool.get("rawArgs") or tool.get("params") or ""
        parts.append(("tool", f"[tool_use {tool['name']}] " + cap(args, TOOL_LIMIT)))
        if tool.get("result"):
            parts.append(("tool", "[tool_result] " + cap(tool["result"], TOOL_LIMIT)))
    return parts


def load_composer_workspaces(cursor_db) -> dict[str, str]:
    """{composer id: workspace id}, for composers that don't record their folder directly."""
    try:
        return dict(cursor_db.execute("select composerId, workspaceId from composerHeaders"))
    except sqlite3.Error:
        return {}


def load_workspace_dirs(workspaces_dir: str) -> dict[str, str]:
    """{workspace id: folder path} from workspaceStorage/<id>/workspace.json."""
    dirs = {}
    for path in glob.glob(os.path.join(workspaces_dir, "*", "workspace.json")):
        try:
            with open(path) as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        uri = meta.get("folder") or meta.get("workspace") or ""
        dirs[os.path.basename(os.path.dirname(path))] = unquote(re.sub(r"^file://", "", uri))
    return dirs


def remove_deleted_composers(db, seen: set[str]) -> None:
    for (key,) in db.execute("select path from files where path like 'cursor-composer:%'").fetchall():
        composer_id = key.split(":", 1)[1]
        if composer_id not in seen:
            store.delete_session(db, f"cursor:{composer_id}")
            store.forget(db, key)


# ---------------------------------------------------------------- agent transcripts

def index_transcripts(db, paths: Paths, full: bool, skip: set[str]) -> int:
    pattern = os.path.join(paths.cursor_projects, "*", "agent-transcripts", "**", "*.jsonl")
    updated = 0
    for path in glob.glob(pattern, recursive=True):
        transcript_id = os.path.splitext(os.path.basename(path))[0]
        if transcript_id in skip:
            continue  # already indexed, with better timestamps, from the composer DB
        stat = os.stat(path)
        if not full and store.is_unchanged(db, path, stat.st_mtime, stat.st_size):
            continue
        store.save_session(db, parse_transcript(path, transcript_id, paths.cursor_projects))
        store.remember(db, path, stat.st_mtime, stat.st_size)
        updated += 1
    return updated


def parse_transcript(path: str, transcript_id: str, projects_dir: str) -> Session:
    session = Session(id=f"cursor:{transcript_id}", source="cursor", native_id=transcript_id, path=path)
    project_slug = os.path.relpath(path, projects_dir).split(os.sep)[0]
    session.cwd = path_from_slug(project_slug)

    # Transcripts carry no per-message times; user turns embed a <timestamp>, which we carry forward.
    current_time = os.path.getmtime(path)
    with open(path, errors="replace") as f:
        for line in f:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            role = record.get("role")
            content = (record.get("message") or {}).get("content")
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
            for block in blocks:
                if block.get("type") == "text":
                    text = block.get("text", "")
                    stamp = re.search(r"<timestamp>(.*?)</timestamp>", text)
                    if stamp:
                        current_time = parse_transcript_time(stamp.group(1)) or current_time
                    text = re.sub(r"</?user_query>|<timestamp>.*?</timestamp>\n?", "", text).strip()
                    session.add(current_time, role, cap(text, TEXT_LIMIT))
                elif block.get("type") == "tool_use":
                    session.add(current_time, "tool", f"[tool_use {block.get('name')}] " + cap(block.get("input"), TOOL_LIMIT))
    return session


def parse_transcript_time(text: str) -> str | None:
    """'Tuesday, Sep 15, 2026, 6:23 PM (UTC-7)' → UTC ISO timestamp."""
    m = re.match(r"\w+, (\w+ \d+, \d{4}), (\d+:\d+ [AP]M) \(UTC([+-]\d+)(?::?(\d+))?\)", text.strip())
    if not m:
        return None
    date, clock, offset_hours, offset_minutes = m.groups()
    try:
        local_time = dt.datetime.strptime(f"{date} {clock}", "%b %d, %Y %I:%M %p")
    except ValueError:
        return None
    sign = 1 if int(offset_hours) >= 0 else -1
    offset = dt.timedelta(hours=int(offset_hours), minutes=sign * int(offset_minutes or 0))
    return iso((local_time - offset).replace(tzinfo=dt.timezone.utc).isoformat())


def path_from_slug(slug: str) -> str:
    """Cursor names project folders like 'Users-me-my-repo' for /Users/me/my-repo. Since '-' is ambiguous,
    rebuild the path by greedily matching the longest component that exists on disk."""
    parts, path, i = slug.split("-"), "/", 0
    while i < len(parts):
        for j in range(len(parts), i, -1):
            candidate = os.path.join(path, "-".join(parts[i:j]))
            if os.path.exists(candidate):
                path, i = candidate, j
                break
        else:
            return os.path.join(path, "-".join(parts[i:]))
    return path
