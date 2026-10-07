"""Codex rollouts: the CLI, the ChatGPT/Codex desktop app, and IDE extensions.

~/.codex/sessions/YYYY/MM/DD/rollout-<time>-<thread id>.jsonl, one record per line:
  {"type": "session_meta", "payload": {"id", "cwd", "parent_thread_id", "agent_nickname", ...}}
  {"type": "response_item", "timestamp", "payload": {"type": "message" | "function_call" | ..., ...}}
Thread titles live separately in state_5.sqlite (threads table) and session_index.jsonl.

Rollouts are large (the first line repeats the full system prompt), so lines are filtered on
their prefix before being JSON-decoded.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sqlite3
from functools import partial

from ..store import Session
from ..util import INJECTED, cap
from . import TEXT_LIMIT, TOOL_LIMIT, Paths, index_files

THREAD_ID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")
TOOL_CALLS = ("function_call", "custom_tool_call", "local_shell_call")
TOOL_OUTPUTS = ("function_call_output", "custom_tool_call_output")


def index(db, paths: Paths, full: bool) -> int:
    threads = load_thread_info(paths.codex_home)
    files = []
    for folder in ("sessions", "archived_sessions"):
        files += glob.glob(os.path.join(paths.codex_home, folder, "**", "*.jsonl"), recursive=True)
    return index_files(db, files, partial(parse, threads=threads), full)


def load_thread_info(codex_home: str) -> dict[str, tuple[str | None, str | None]]:
    """{thread id: (title, git branch)} from Codex's state DB, falling back to session_index.jsonl."""
    threads = {}
    try:
        state = sqlite3.connect(f"file:{codex_home}/state_5.sqlite?immutable=1", uri=True)
        for thread_id, title, name, branch in state.execute("select id, title, name, git_branch from threads"):
            threads[thread_id] = (name or title, branch)
    except sqlite3.Error:
        pass
    try:
        with open(os.path.join(codex_home, "session_index.jsonl")) as f:
            for line in f:
                entry = json.loads(line)
                if entry.get("thread_name"):
                    threads.setdefault(entry["id"], (entry["thread_name"], None))
    except (OSError, ValueError):
        pass
    return threads


def parse(path: str, threads: dict) -> Session:
    stem = os.path.splitext(os.path.basename(path))[0]
    match = THREAD_ID.search(stem)
    thread_id = match.group(1) if match else stem
    session = Session(id=f"codex:{thread_id}", source="codex", native_id=thread_id, path=path)

    with open(path, errors="replace") as f:
        for line in f:
            head = line[:120]
            if '"session_meta"' in head:
                read_session_meta(session, json.loads(line).get("payload", {}))
            elif '"type":"turn_context"' in head:
                session.cwd = session.cwd or json.loads(line).get("payload", {}).get("cwd")
            elif '"response_item"' in head:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                read_response_item(session, record.get("payload", {}), record.get("timestamp"))

    title, branch = threads.get(session.native_id, (None, None))
    session.title = title or session.title
    session.branch = branch or session.branch
    return session


def read_session_meta(session: Session, meta: dict) -> None:
    session.cwd = session.cwd or meta.get("cwd")
    session.native_id = meta.get("id") or session.native_id
    session.parent = meta.get("parent_thread_id")
    if meta.get("agent_nickname"):
        session.title = session.title or f"[subagent {meta['agent_nickname']}]"


def read_response_item(session: Session, item: dict, timestamp) -> None:
    kind = item.get("type")
    if kind == "message" and item.get("role") in ("user", "assistant"):
        text = "\n".join(c.get("text", "") for c in item.get("content") or [] if isinstance(c, dict))
        if item["role"] == "user" and INJECTED.match(text):
            return  # environment/AGENTS.md blocks Codex injects as user messages
        session.add(timestamp, item["role"], cap(text, TEXT_LIMIT))
    elif kind in TOOL_CALLS:
        args = item.get("arguments") or item.get("input") or item.get("action")
        session.add(timestamp, "tool", f"[tool_use {item.get('name', kind)}] " + cap(args, TOOL_LIMIT))
    elif kind in TOOL_OUTPUTS:
        output = item.get("output")
        if isinstance(output, dict):
            output = output.get("content") or output
        session.add(timestamp, "tool", "[tool_result] " + cap(output or "", TOOL_LIMIT))
