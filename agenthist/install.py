"""Install or remove the hist hooks in each client's user-level config.

Edits are merges: other hooks are left alone, ours are recognized by their `hist hook` command, and the
original file is backed up next to itself before the first write.
"""
from __future__ import annotations

import copy
import difflib
import json
import os
import shlex
import shutil
import time

from .util import HOME

CLAUDE_SETTINGS = os.path.join(HOME, ".claude", "settings.json")
CODEX_HOOKS = os.path.join(HOME, ".codex", "hooks.json")
CURSOR_HOOKS = os.path.join(HOME, ".cursor", "hooks.json")

MARKER = " hook "  # every command we install is `<python> <hist> hook <event> --client <client>`


def hist_command(event: str, client: str) -> str:
    """An absolute command, so hooks work regardless of the client's PATH."""
    launcher = os.path.join(HOME, ".local", "bin", "hist")
    if not os.path.exists(launcher):
        launcher = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "hist"))
    return f"/usr/bin/python3 {shlex.quote(launcher)}{MARKER}{event} --client {client}"


def is_ours(command: str) -> bool:
    return "hist" in command and MARKER in command and "--client" in command


# ---------------------------------------------------------------- per-client edits

def claude_style(config: dict, client: str, install: bool) -> dict:
    """Claude Code settings.json and Codex hooks.json share this shape:
    {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": ...}]}]}}"""
    config = copy.deepcopy(config)
    hooks = config.setdefault("hooks", {})
    for event_name, event in (("UserPromptSubmit", "prompt"), ("Stop", "stop")):
        groups = [g for g in hooks.get(event_name, []) if not any(is_ours(h.get("command", "")) for h in g.get("hooks", []))]
        if install:
            groups.append({"hooks": [{"type": "command", "command": hist_command(event, client), "timeout": 30}]})
        if groups:
            hooks[event_name] = groups
        else:
            hooks.pop(event_name, None)
    if not hooks:
        config.pop("hooks")
    return config


def cursor_style(config: dict, install: bool) -> dict:
    """Cursor's hooks.json: {"version": 1, "hooks": {"stop": [{"command": ..., "loop_limit": 1}]}}"""
    config = copy.deepcopy(config) or {}
    config.setdefault("version", 1)
    hooks = config.setdefault("hooks", {})
    for event_name, event, extra in (("beforeSubmitPrompt", "prompt", {}), ("stop", "stop", {"loop_limit": 1})):
        entries = [h for h in hooks.get(event_name, []) if not is_ours(h.get("command", ""))]
        if install:
            entries.append({"command": hist_command(event, "cursor"), "timeout": 30, **extra})
        if entries:
            hooks[event_name] = entries
        else:
            hooks.pop(event_name, None)
    if not hooks and set(config) <= {"version", "hooks"}:
        return {}  # nothing left but our scaffolding: remove the file
    return config


TARGETS = {
    "claude": (CLAUDE_SETTINGS, lambda config, install: claude_style(config, "claude", install)),
    "codex": (CODEX_HOOKS, lambda config, install: claude_style(config, "codex", install)),
    "cursor": (CURSOR_HOOKS, cursor_style),
}


# ---------------------------------------------------------------- apply

def plan(client: str, install: bool) -> tuple[str, str, str]:
    """(path, current text, proposed text) for one client."""
    path, edit = TARGETS[client]
    try:
        with open(path) as f:
            current_text = f.read()
    except FileNotFoundError:
        current_text = ""
    current = json.loads(current_text) if current_text.strip() else {}
    proposed = edit(current, install)
    proposed_text = json.dumps(proposed, indent=2, ensure_ascii=False) + "\n" if proposed else ""
    return path, current_text, proposed_text


def diff(path: str, before: str, after: str) -> str:
    lines = difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                 fromfile=path, tofile=path + " (new)")
    return "".join(lines)


def apply(path: str, before: str, after: str) -> str | None:
    """Write the new config, backing up the old one. Returns the backup path, if one was made."""
    backup = None
    if before:
        backup = f"{path}.hist-backup-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(path, backup)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if after:
        with open(path, "w") as f:
            f.write(after)
    elif os.path.exists(path):
        os.remove(path)
    return backup
