"""Lifecycle hooks for Claude Code, Codex and Cursor: `hist hook <prompt|stop> --client <name>`.

prompt  Records a snapshot of the working tree (so the stop hook knows what this turn changed), and adds
        any strongly relevant cm playbook rules to the agent's context.
stop    The merge-ready gate. If this turn changed code and the scan finds something, the agent is sent back
        once with a short checklist; the next stop always goes through.

The gate reads the turn's own transcript where it can, so it only asks about work this agent did: when
another agent is editing the same checkout (an orchestrator waiting on a background Codex task, say),
files the turn's tools never touched are left to that agent's own gate, and a CI command the agent
already ran after its last edit isn't asked for again.

Hooks must never break the agent: every failure is logged to hooks.log and the hook exits quietly.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import traceback

from . import ready
from .util import HOME, iso

STATE_DIR = os.path.join(HOME, ".cache", "agent-history", "turns")
LOG_PATH = os.path.join(HOME, ".cache", "agent-history", "hooks.log")
CLIENTS = ("claude", "codex", "cursor")

PROMPT_RULE_LIMIT = 3      # cm rules injected at prompt time
STOP_RULE_LIMIT = 5        # cm rules listed in the stop checklist
LIST_LIMIT = 6             # findings shown per category
CONCURRENT_WINDOW = 6 * 3600   # a turn with no recorded stop counts as still running for this long...
IDLE_LIMIT = 15 * 60           # ...unless its transcript hasn't been written to for this long (killed, aborted)
TRANSCRIPT_TAIL = 20 * 2**20   # bytes of transcript read at stop time

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
PATCH_FILE = re.compile(r"\*\*\* (?:Update|Add|Delete) File: (\S+)")
# Shell commands that write files, and ones that hand work to another agent (whose edits aren't ours).
SHELL_WRITE = re.compile(r"(?<![<>=&2-])>(?![&>]|\s*/dev/null)|\bsed -i|\bperl -\w*i|\btee\b|\.write\(|open\([^)]*['\"][wa]|"
                         r"\b(mv|cp|rm|touch|git (mv|rm|checkout|restore|apply))\b")
DELEGATION = re.compile(r"codex-companion|\bcodex (exec|e)\b|\bclaude -p\b|\bcursor-agent\b")


def main(event: str, client: str) -> None:
    started = time.time()
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        turn = Turn.from_payload(client, payload)
        if event == "prompt":
            output = on_prompt(turn)
        elif event == "stop":
            output = on_stop(turn)
        else:
            output = None
        if output:
            print(json.dumps(output))
        log({"event": event, "client": client, "session": turn.session, "cwd": turn.cwd,
             "fired": bool(output), **turn.notes, "ms": round((time.time() - started) * 1000)})
    except Exception:  # noqa: BLE001 — a hook must fail open
        log({"event": event, "client": client, "error": traceback.format_exc(limit=5)})


class Turn:
    """The parts of a hook payload we use, normalized across clients."""

    def __init__(self, client: str, session: str, cwd: str | None, prompt: str | None, stop_active: bool,
                 aborted: bool, transcript: str | None = None):
        self.client, self.session, self.cwd = client, session, cwd
        self.prompt, self.stop_active, self.aborted = prompt, stop_active, aborted
        self.transcript = transcript
        self.notes: dict = {}  # what the hook decided and why, for hooks.log

    @classmethod
    def from_payload(cls, client: str, p: dict) -> "Turn":
        if client == "cursor":
            roots = p.get("workspace_roots") or []
            return cls(client, p.get("conversation_id") or "unknown", roots[0] if roots else p.get("cwd"),
                       p.get("prompt"), stop_active=(p.get("loop_count") or 0) > 0,
                       aborted=p.get("status") not in (None, "completed"), transcript=p.get("transcript_path"))
        # Claude Code and Codex share a payload shape.
        return cls(client, p.get("session_id") or "unknown", p.get("cwd"), p.get("prompt"),
                   stop_active=bool(p.get("stop_hook_active")), aborted=False, transcript=p.get("transcript_path"))

    @property
    def state_path(self) -> str:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in self.session)
        return os.path.join(STATE_DIR, f"{self.client}-{safe}.json")

    def save(self, root: str) -> None:
        self.write({"prompt": self.prompt, "root": root, "snapshot": ready.snapshot(root), "ts": iso(time.time()),
                    "transcript": self.transcript})

    def mark_stopped(self, state: dict) -> None:
        self.write({**state, "stopped": iso(time.time())})

    def write(self, state: dict) -> None:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(self.state_path, "w") as f:
            json.dump(state, f)

    def load(self) -> dict | None:
        try:
            with open(self.state_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None


# ---------------------------------------------------------------- events

def on_prompt(turn: Turn) -> dict | None:
    root = ready.repo_root(turn.cwd)
    if root:
        turn.save(root)
    rules = cm_rules(turn.prompt, turn.cwd, PROMPT_RULE_LIMIT, proven_only=True) if turn.prompt else []
    if not rules:
        return prompt_output(turn.client, None)
    text = "Rules from the user's playbook that apply to this request:\n" + "\n".join(f"- {r}" for r in rules)
    return prompt_output(turn.client, text)


def on_stop(turn: Turn) -> dict | None:
    if turn.stop_active or turn.aborted:
        return None  # already sent back once this turn (or the user stopped it): let it finish
    root = ready.repo_root(turn.cwd)
    if not root:
        return None

    state = turn.load()
    if state and state.get("root") == root:
        files, prompt, since = ready.changed_since(root, state["snapshot"]), state.get("prompt"), state.get("ts")
        turn.mark_stopped(state)
    else:  # no snapshot (hook installed mid-session): fall back to uncommitted work
        files, prompt, since = ready.dirty_files(root), turn.prompt, None
    turn.notes["changed"] = len(files)
    if not files:
        return None  # a question or a read-only turn: nothing to gate

    activity = Activity.read(turn.transcript, since) if since else None
    if activity and not activity.calls:
        turn.notes["no_tool_calls"] = True
        return None  # the agent only talked: whatever changed was done by someone else (a subagent, the user)
    others = concurrent_turns(turn, root, since) if since else []
    if others and activity:
        # Someone else is editing this checkout: only ask about files this turn's own tools wrote.
        files = [path for path in files if activity.wrote(root, path)]
        turn.notes.update(concurrent=others, own=len(files))
        if not files:
            return None

    report = ready.scan(root, files=files, prompt=prompt)
    if activity:
        report.checks = [command for command in report.checks if not activity.ran_after_last_edit(command)]
    rules = cm_rules(prompt or "", root, STOP_RULE_LIMIT) if prompt else []
    text = checklist(report, rules)
    turn.notes["items"] = sorted({f.kind for f in report.findings}) + (["checks"] if report.checks else [])
    if not text:
        return None
    return stop_output(turn.client, text)


# ---------------------------------------------------------------- what this turn did

class Activity:
    """The tool calls this agent made during the turn, read from its transcript (Claude Code or Codex)."""

    def __init__(self):
        self.calls: list[tuple[set[str], str]] = []  # (files the call edited, its input as text), in order
        self.recognized = False  # saw this turn's assistant entries, so an empty `calls` really means none

    @classmethod
    def read(cls, transcript: str | None, since: str) -> "Activity | None":
        if not transcript or not os.path.isfile(transcript):
            return None
        activity = cls()
        try:
            with open(transcript, "rb") as f:
                f.seek(max(0, os.path.getsize(transcript) - TRANSCRIPT_TAIL))
                lines = f.read().decode("utf-8", errors="replace").splitlines()
        except OSError:
            return None
        for line in lines:
            if '"assistant"' not in line and '"response_item"' not in line:
                continue  # cheap pre-filter: tool calls live in assistant entries (Claude) or response items (Codex)
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if (entry.get("timestamp") or "") >= since:
                activity.add(entry)
        return activity if activity.recognized else None

    def add(self, entry: dict) -> None:
        self.recognized |= entry.get("type") in ("assistant", "response_item")
        content = (entry.get("message") or {}).get("content") if entry.get("type") == "assistant" else None
        for block in content if isinstance(content, list) else []:  # Claude Code
            if block.get("type") == "tool_use":
                tool_input = block.get("input") or {}
                path = tool_input.get("file_path") or tool_input.get("notebook_path")
                edited = {path} if block.get("name") in EDIT_TOOLS and path else set()
                self.calls.append((edited, json.dumps(tool_input)))
        payload = entry.get("payload") or {}
        if entry.get("type") == "response_item" and payload.get("type", "").endswith("_call"):  # Codex
            text = payload.get("input") or payload.get("arguments") or json.dumps(payload.get("action") or {})
            text = text if isinstance(text, str) else json.dumps(text)
            edited = {path.split("\\n")[0] for path in PATCH_FILE.findall(text)}  # patches inside JS strings keep "\n"
            self.calls.append((edited, text))

    def wrote(self, root: str, path: str) -> bool:
        """Did one of this turn's tool calls write `path` (relative to the repo root)?"""
        absolute = os.path.join(root, path)
        for edited, text in self.calls:
            if any(e == path or e == absolute or e.endswith("/" + path) or path.endswith("/" + e) for e in edited):
                return True
            if (path in text or absolute in text) and SHELL_WRITE.search(text) and not DELEGATION.search(text):
                return True
        return False

    def ran_after_last_edit(self, command: str) -> bool:
        """Did the agent run this check command after its last file edit (by an edit tool or the shell)?"""
        last_run = max((n for n, (_, text) in enumerate(self.calls) if command_ran(command, text)), default=-1)
        return last_run >= 0 and not any(edited or (SHELL_WRITE.search(text) and not DELEGATION.search(text))
                                         for edited, text in self.calls[last_run + 1:])


def command_ran(command: str, text: str) -> bool:
    """Loose match, so `pnpm -C web check:all` and `cd frontend && (cd web && pnpm check:all)` count for
    `cd frontend/web && CI=true pnpm check:all`."""
    if command in text:
        return True
    directory, _, rest = command.rpartition(" && ")
    script = rest.split()[-1] if rest.split() else rest
    package = os.path.basename(directory[3:].strip().rstrip("/")) if directory.startswith("cd ") else ""
    return bool(script) and re.search(rf"(?<![\w:-]){re.escape(script)}(?![\w:-])", text) is not None and \
        (not package or package in text)


def concurrent_turns(turn: Turn, root: str, since: str) -> list[str]:
    """Other agent sessions (any client) whose turns in this checkout overlap this one."""
    stale = iso(time.time() - CONCURRENT_WINDOW)
    others = []
    try:
        names = os.listdir(STATE_DIR)
    except OSError:
        return []
    for name in names:
        path = os.path.join(STATE_DIR, name)
        if path == turn.state_path or not name.endswith(".json"):
            continue
        try:
            with open(path) as f:
                other = json.load(f)
        except (OSError, ValueError):
            continue
        started, stopped = other.get("ts") or "", other.get("stopped")
        if other.get("root") != root or started < stale:
            continue
        if stopped is None and idle(other.get("transcript")):
            continue  # never stopped, but nothing has happened in it for a while
        if stopped is None or stopped >= since:  # still running, or finished during our turn
            others.append(name[:-len(".json")])
    return others


def idle(transcript: str | None) -> bool:
    try:
        return time.time() - os.path.getmtime(transcript) > IDLE_LIMIT
    except (OSError, TypeError):
        return False


# ---------------------------------------------------------------- output

def checklist(report: ready.Report, rules: list[str]) -> str | None:
    """The message the agent gets when it tries to finish. None when there is nothing worth raising."""
    items = []

    out_of_scope = report.by_kind("scope")
    if out_of_scope:
        named = ", ".join(report.named_paths[:4])
        items.append(f"Scope: the request named {named}, but this turn also changed "
                     f"{list_paths(out_of_scope)}. Revert anything the request doesn't need.")
    comments = report.by_kind("comment")
    if comments:
        items.append("Comments that narrate the edit rather than explain the code (remove them): "
                     + list_lines(comments))
    tests = report.by_kind("test-file")
    if tests:
        items.append(f"New test/story files: {list_paths(tests)}. Keep them only if they were asked for or prove the change.")
    debug = report.by_kind("debug")
    if debug:
        items.append("Debug output left in: " + list_lines(debug))
    todos = report.by_kind("todo")
    if todos:
        items.append("TODO/FIXME added: " + list_lines(todos))
    if not items and not rules and not report.checks:
        return None  # a clean change with no CI command to run: let it finish

    if report.checks:
        commands = " and ".join(f"`{command}`" for command in report.checks)
        items.append(f"Checks: run {commands} (what CI runs) and report the result, or say why you didn't.")

    lines = ["Before finishing, go through these and address each in your final message (fix it, or say why it stays):"]
    lines += [f"{n}. {item}" for n, item in enumerate(items, 1)]
    if rules:
        lines.append("From the user's playbook (cm):")
        lines += [f"- {rule}" for rule in rules]
    lines.append("(hist merge-ready gate — runs once per turn)")
    return "\n".join(lines)


def list_paths(findings: list[ready.Finding]) -> str:
    paths = list(dict.fromkeys(f.path for f in findings))
    more = f" (+{len(paths) - LIST_LIMIT} more)" if len(paths) > LIST_LIMIT else ""
    return ", ".join(paths[:LIST_LIMIT]) + more


def list_lines(findings: list[ready.Finding]) -> str:
    shown = [f"{f.path}:{f.line} `{f.text}`" for f in findings[:LIST_LIMIT]]
    more = f" (+{len(findings) - LIST_LIMIT} more)" if len(findings) > LIST_LIMIT else ""
    return "; ".join(shown) + more


def prompt_output(client: str, context: str | None) -> dict | None:
    if client == "cursor":
        return {"continue": True}  # Cursor's beforeSubmitPrompt can't add context; we only record the snapshot
    if not context:
        return None
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}


def stop_output(client: str, text: str) -> dict:
    if client == "cursor":
        return {"followup_message": text}
    return {"decision": "block", "reason": text}  # Claude Code and Codex


# ---------------------------------------------------------------- cm (procedural memory)

def cm_rules(task: str, workspace: str | None, limit: int, proven_only: bool = False) -> list[str]:
    """Relevant rules and anti-patterns from `cm context`. Empty when cm is missing, slow, or has nothing."""
    if not cm_has_rules(workspace):
        return []  # skip `cm context`, which spends seconds searching history before reading an empty playbook
    command = ["cm", "context", task[:2000], "--json", "--limit", str(limit), "--history", "1", "--days", "1"]
    if workspace:
        command += ["--workspace", workspace]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=6)
        data = json.loads(result.stdout).get("data") or {}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []

    rules = []
    for bullet in data.get("relevantBullets") or []:
        if proven_only and bullet.get("maturity") not in ("proven", "established"):
            continue
        rules.append(bullet.get("content", "").strip())
    for bullet in data.get("antiPatterns") or []:
        rules.append("Avoid: " + bullet.get("content", "").strip())
    return [rule for rule in rules if rule][:limit]


def cm_has_rules(workspace: str | None) -> bool:
    """Whether the global or workspace playbook has any rules, read from the files (no process spawn)."""
    playbooks = [os.path.join(HOME, ".cass-memory", "playbook.yaml")]
    root = ready.repo_root(workspace)
    if root:
        playbooks.append(os.path.join(root, ".cass", "playbook.yaml"))
    for path in playbooks:
        try:
            with open(path) as f:
                text = f.read()
        except OSError:
            continue
        if re.search(r"^bullets:\s*(\n\s*-|\[\s*[^\]\s])", text, re.M):
            return True
    return False


def log(entry: dict) -> None:
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps({"ts": iso(time.time()), **entry}) + "\n")
    except OSError:
        pass
