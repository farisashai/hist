"""Devin: cloud sessions as cached by the Devin desktop app.

The desktop app (a Windsurf build) talks to every agent over ACP and caches each session's messages in its
own SQLite file, ~/Library/Application Support/Devin/User/acp-messages/<sha1 of the session key>.db:
  meta(key, value)                     → info: {title, _meta: {"cognition.ai/url", sessionRepos, sessionPRs, ...}}
  messages(position, kind, payload)    → kind: user_message | agent_message | agent_thought | tool_call | plan | ...
Positions can be negative: older history is backfilled in front of what was cached first.

User/globalStorage/state.vscdb maps session keys to those files ("windsurf.acp.messageStore.session.<key>"):
  devin-session-cache/1/<user>/<account>/devin-cloud/devin-<id>   a Devin cloud session (indexed here)
  acp/codex-acp/<thread id>, acp/claude-acp/<session id>         Codex / Claude Code run inside the app; their
                                                                  own transcripts are indexed, so these are skipped
  acp/devin-cli/<name>                                            a local Devin CLI session (indexed here)

Cloud sessions run on Devin's VMs, so they have no local directory; their cwd is your own checkout of the
session's repo, found among the directories your other agents ran in.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from collections import Counter

from .. import store
from ..store import Session
from ..util import cap
from . import TEXT_LIMIT, TOOL_LIMIT, Paths, open_readonly

STORE_PREFIX = "windsurf.acp.messageStore.session."
CLOUD_URL = "https://app.devin.ai/sessions/"
CLOUD_ID = re.compile(r"[0-9a-f]{32}")
# Clients whose sessions hist reads from their own transcripts; the app's copy would be a duplicate.
INDEXED_ELSEWHERE = ("acp/codex-acp/", "acp/claude-acp/")
# Bookkeeping events that carry no content of the conversation.
NOISE_EVENTS = {"context_growth_update", "acu_consumption_at_last_user_interaction", "session_analysis",
                "attachment_live_update", "first_session_message"}


def index(db, paths: Paths, full: bool) -> int:
    if not os.path.isdir(paths.devin_messages):
        return 0
    keys = load_session_keys(paths.devin_state_db)
    checkouts = None  # built on first use: it walks the directories of every other indexed session
    updated = 0
    for path in glob.glob(os.path.join(paths.devin_messages, "*.db")):
        mtime, size = db_stat(path)
        if mtime is None or (not full and store.is_unchanged(db, path, mtime, size)):
            continue
        key = keys.get(os.path.splitext(os.path.basename(path))[0], "")
        if key.startswith(INDEXED_ELSEWHERE):
            store.remember(db, path, mtime, size)
            continue
        try:
            session, repos = parse(path, key)
            if not key and not CLOUD_ID.fullmatch(session.native_id):
                session.messages = []  # not in the app's index and not a cloud session: can't tell which agent it is
            if session.cwd is None and repos:
                checkouts = checkouts if checkouts is not None else local_checkouts(db)
                session.cwd = next((checkouts[r.lower()] for r in repos if r.lower() in checkouts), None)
            store.save_session(db, session)
        except Exception as e:  # one unreadable cache file must not break the whole index
            print(f"hist: skipped {path}: {e}", file=sys.stderr)
        store.remember(db, path, mtime, size)
        updated += 1
    return updated


def db_stat(path: str) -> tuple[float | None, int]:
    """The file's change stamp. The app writes through a WAL, so the main file alone can look unchanged."""
    mtime, size = None, 0
    for part in (path, path + "-wal"):
        try:
            stat = os.stat(part)
        except OSError:
            continue
        mtime, size = max(mtime or 0, stat.st_mtime), size + stat.st_size
    return mtime, size


def load_session_keys(state_db: str) -> dict[str, str]:
    """{cache file name: session key} from the app's message-store index."""
    try:
        state = open_readonly(state_db, "select 1 from ItemTable limit 1")
        rows = state.execute("select key, value from ItemTable where key >= ? and key < ?",
                             (STORE_PREFIX, STORE_PREFIX[:-1] + "/"))
    except Exception:
        return {}
    keys = {}
    for key, value in rows:
        try:
            keys[json.loads(value)["uuid"]] = key[len(STORE_PREFIX):]
        except (ValueError, KeyError, TypeError):
            continue
    return keys


# ---------------------------------------------------------------- one session

def parse(path: str, key: str) -> tuple[Session, list[str]]:
    """The session, and the repos ("owner/name") a cloud session worked in, to find your local checkout."""
    cache = open_readonly(path, "select 1 from messages limit 1")
    cache.text_factory = lambda b: b.decode("utf-8", "replace")
    meta = dict(cache.execute("select key, value from meta"))
    info = loads(meta.get("info")) or {}
    extra = info.get("_meta") or {}

    native_id = session_id(extra.get("cognition.ai/url"), key, path)
    session = Session(id=f"devin:{native_id}", source="devin", native_id=native_id, path=path)
    session.title = info.get("title") or None
    repos = [r.get("name") or r.get("id") for r in extra.get("cognition.ai/sessionRepos") or [] if isinstance(r, dict)]
    prs = [pr for pr in extra.get("cognition.ai/sessionPRs") or [] if isinstance(pr, dict)]
    session.native_summary = pr_summary(prs)

    # Local (CLI) sessions carry no per-message times: carry the last one seen forward from the file's mtime.
    current_time = db_stat(path)[0]
    for _, kind, payload in cache.execute("select position, kind, payload from messages order by position"):
        record = loads(payload)
        if not isinstance(record, dict):
            continue
        current_time = record_time(record) or current_time
        for role, text in record_parts(kind, record):
            session.add(current_time, role, text)
        if kind == "tool_call" and not session.cwd:
            session.cwd = local_cwd(record)
        session.note_branch(record_branch(kind, record))
    for pr in prs:  # the PRs it ended up with, in case their creation is outside the cached history
        session.note_branch(pr.get("headRef"))
    return session, [r for r in repos if r]


def record_branch(kind: str, record: dict) -> str | None:
    """The branch a PR was opened or taken over on, or pushed to."""
    if kind == "agent_message":
        return block_meta(record).get("cognition.ai/branchName")
    if kind == "tool_call":
        meta = (record.get("content") or {}).get("_meta") or {}
        if meta.get("cognition.ai/eventType") == "git_push":
            return meta.get("cognition.ai/branch")
    return None


def session_id(url: str | None, key: str, path: str) -> str:
    """The id app.devin.ai uses (…/sessions/<id>), else the session key's last part, else the file name."""
    if url and url.startswith(CLOUD_URL):
        return url[len(CLOUD_URL):].strip("/")
    last = key.rsplit("/", 1)[-1]
    if last:
        return last[len("devin-"):] if last.startswith("devin-") else last
    return os.path.splitext(os.path.basename(path))[0]


def pr_summary(prs: list[dict]) -> str | None:
    """'PR #15868 (open): Allow cloud agents to read …; PR #15870 (merged): …' — what the session produced."""
    parts = []
    for pr in prs:
        number = (pr.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
        state = "merged" if pr.get("merged") or pr.get("state") == "merged" else pr.get("state") or "?"
        parts.append(f"PR #{number} ({state}): {pr.get('title') or ''}".strip())
    return "; ".join(parts) or None


def record_parts(kind: str, record: dict) -> list[tuple[str, str]]:
    if kind in ("user_message", "agent_message"):
        role = "user" if kind == "user_message" else "assistant"
        return [(role, cap(message_text(record), TEXT_LIMIT))]
    if kind == "plan":
        entries = (record.get("content") or {}).get("entries") or []
        lines = [f"[{e.get('status', '?')}] {e.get('content', '')}" for e in entries if isinstance(e, dict)]
        return [("tool", "[plan] " + cap("\n".join(lines), TOOL_LIMIT))] if lines else []
    if kind == "tool_call":
        return tool_parts(record.get("content") or {})
    return []  # agent_thought (reasoning and one-line status), progress_marker, unknown notices


def message_text(record: dict) -> str:
    """A user/agent message's text. Its blocks are chunks of one message; hidden resources (citations) are skipped."""
    blocks = record.get("content") if isinstance(record.get("content"), list) else []
    return "".join(b["content"].get("text") or "" for b in blocks
                   if isinstance(b, dict) and isinstance(b.get("content"), dict) and b["content"].get("type") == "text")


def tool_parts(call: dict) -> list[tuple[str, str]]:
    meta = call.get("_meta") or {}
    event = meta.get("cognition.ai/eventType")
    if event in NOISE_EVENTS or (event == "terminal_update" and not call.get("kind")):
        return []
    kind = call.get("kind")  # execute | read | edit | search | other
    name = call.get("name") or meta.get("cognition.ai/toolName") or (kind if kind != "other" else None) or event or "tool"
    args = call.get("title") or ""
    raw_input = call.get("rawInput")
    if raw_input and raw_input != args and not (isinstance(raw_input, dict) and raw_input.get("command") == args):
        args = f"{args} {cap(raw_input, TOOL_LIMIT)}".strip()
    parts = [("tool", f"[tool_use {name}] " + cap(args, TOOL_LIMIT))]
    result = call.get("rawOutput") or content_text(call.get("content"))
    if result:
        parts.append(("tool", "[tool_result] " + cap(result, TOOL_LIMIT)))
    return parts


def content_text(items) -> str:
    """Readable text from a tool call's content: text and links (one sentence, in pieces) and edited paths.
    Terminal output is only kept as a remote blob, so it is skipped."""
    out = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "diff":
            out += [f"\n{c.get('operation', 'edit')} {c.get('path')}" for c in item.get("changes") or [] if isinstance(c, dict)]
        inner = item.get("content")
        if item.get("type") == "content" and isinstance(inner, dict):
            if inner.get("type") == "text":
                out.append(inner.get("text") or "")
            elif inner.get("type") == "resource_link" and str(inner.get("uri", "")).startswith("http"):
                out.append(f"[{inner.get('name')}]({inner['uri']})")
    return "".join(out).strip()


def record_time(record: dict):
    for meta in (block_meta(record), (record.get("content") or {}).get("_meta") if isinstance(record.get("content"), dict) else None):
        if meta and (meta.get("cognition.ai/timestamp") or meta.get("cognition.ai/createdAtMs")):
            return meta.get("cognition.ai/timestamp") or meta.get("cognition.ai/createdAtMs")
    return record.get("sourceEventTimestampMs") or (record.get("marker") or {}).get("timestampMs")


def block_meta(record: dict) -> dict:
    """The _meta of a message's first content block."""
    blocks = record.get("content")
    if isinstance(blocks, list) and blocks and isinstance(blocks[0], dict):
        return blocks[0].get("_meta") or {}
    return {}


def local_cwd(record: dict) -> str | None:
    """A local session's directory, from a tool call that ran on this machine."""
    raw_input = (record.get("content") or {}).get("rawInput")
    cwd = raw_input.get("cwd") if isinstance(raw_input, dict) else None
    return cwd if cwd and os.path.isdir(cwd) else None


def loads(value):
    try:
        return json.loads(value) if value else None
    except ValueError:
        return None


# ---------------------------------------------------------------- repo → local checkout

REMOTE_URL = re.compile(r"^\s*url\s*=\s*\S*?[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?\s*$", re.M)


def local_checkouts(db) -> dict[str, str]:
    """{'owner/name' (lowercase): local checkout}, from the directories your other agents ran in; the checkout
    with the most sessions wins when a repo is cloned more than once."""
    sessions_per_root, roots = Counter(), {}
    for cwd, count in db.execute("select cwd, count(*) from sessions where cwd is not null and source != 'devin' group by cwd"):
        root = git_root(cwd, roots)
        if root:
            sessions_per_root[root] += count
    checkouts = {}
    for root, _ in sessions_per_root.most_common():
        try:
            with open(os.path.join(root, ".git", "config"), errors="replace") as f:
                config = f.read()
        except OSError:
            continue
        for slug in REMOTE_URL.findall(config):
            checkouts.setdefault(slug.lower(), root)
    return checkouts


def git_root(path: str, cache: dict) -> str | None:
    """The enclosing main checkout (a .git directory; worktrees keep their remotes elsewhere)."""
    seen = []
    while path and path not in cache:
        seen.append(path)
        if os.path.isdir(os.path.join(path, ".git")):
            cache[path] = path
            break
        parent = os.path.dirname(path)
        if parent == path:
            cache[path] = None
            break
        path = parent
    root = cache.get(path)
    for p in seen:
        cache[p] = root
    return root
