"""Claude Code transcripts: the CLI, IDE extensions, the desktop app's Code tab, and Cowork sessions.

All of them write the same JSONL format, one record per line:
  {"type": "user" | "assistant", "timestamp", "cwd", "gitBranch", "message": {"role", "content"}}
  {"type": "ai-title", "aiTitle"} / {"type": "custom-title", "customTitle"} / {"type": "summary", "summary"}
"""
from __future__ import annotations

import glob
import json
import os
from functools import partial

from ..store import Session
from ..util import cap
from . import TEXT_LIMIT, TOOL_LIMIT, Paths, index_files


def index(db, paths: Paths, full: bool) -> int:
    desktop_titles = load_desktop_titles(paths.claude_desktop)
    cli_files = glob.glob(os.path.join(paths.claude_projects, "**", "*.jsonl"), recursive=True)
    cowork_files = glob.glob(os.path.join(paths.claude_desktop, "local-agent-mode-sessions", "**",
                                          ".claude", "projects", "**", "*.jsonl"), recursive=True)
    return (index_files(db, cli_files, partial(parse, source="claude", desktop_titles=desktop_titles), full)
            + index_files(db, cowork_files, partial(parse, source="claude-desktop", desktop_titles=desktop_titles), full))


def load_desktop_titles(desktop_dir: str) -> dict[str, tuple[str, str | None]]:
    """The desktop app keeps a title per session in local_*.json files: {cli session id: (title, cwd)}."""
    titles = {}
    for path in glob.glob(os.path.join(desktop_dir, "*-sessions", "*", "*", "local_*.json")):
        try:
            with open(path) as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        if meta.get("cliSessionId") and meta.get("title"):
            titles[meta["cliSessionId"]] = (meta["title"], meta.get("cwd"))
    return titles


def parse(path: str, source: str, desktop_titles: dict) -> Session:
    native_id = os.path.splitext(os.path.basename(path))[0]
    session = Session(id=f"{source}:{native_id}", source=source, native_id=native_id, path=path)
    if "/subagents/" in path:  # <project>/<parent session id>/subagents/agent-*.jsonl
        session.parent = path.split("/subagents/")[0].rsplit("/", 1)[-1]

    with open(path, errors="replace") as f:
        for line in f:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            read_record(session, record)

    if native_id in desktop_titles:
        title, cwd = desktop_titles[native_id]
        session.title = title
        if source == "claude-desktop" and cwd:
            session.cwd = cwd
    return session


def read_record(session: Session, record: dict) -> None:
    kind = record.get("type")
    if kind == "ai-title" and not session.title:
        session.title = record.get("aiTitle")
    elif kind == "custom-title":
        session.title = record.get("customTitle")
    elif kind == "summary":
        session.native_summary = record.get("summary")
    if kind not in ("user", "assistant"):
        return

    session.cwd = session.cwd or record.get("cwd")
    session.branch = session.branch or record.get("gitBranch")
    message = record.get("message") or {}
    content = message.get("content")

    if record.get("isCompactSummary"):  # the summary Claude writes when it compacts a long conversation
        session.native_summary = cap(content if isinstance(content, str) else join_text(content), 1500)
        return
    if record.get("isMeta"):
        return

    timestamp = record.get("timestamp")
    for role, text in content_parts(content, message.get("role") or kind):
        session.add(timestamp, role, text)


def content_parts(content, role: str):
    """Yield (role, text) for a message's content: plain text, or blocks of text / tool_use / tool_result."""
    if isinstance(content, str):
        yield role, cap(content, TEXT_LIMIT)
        return
    for block in content or []:
        kind = block.get("type")
        if kind == "text":
            yield role, cap(block.get("text", ""), TEXT_LIMIT)
        elif kind == "tool_use":
            yield "tool", f"[tool_use {block.get('name')}] " + cap(block.get("input"), TOOL_LIMIT)
        elif kind == "tool_result":
            result = block.get("content")
            if isinstance(result, list):
                result = join_text(result, sep="\n")
            yield "tool", "[tool_result] " + cap(result or "", TOOL_LIMIT)


def join_text(blocks, sep: str = "") -> str:
    return sep.join(b.get("text", "") for b in blocks or [] if isinstance(b, dict))
