"""On-demand LLM titles/summaries: send a condensed transcript to a CLI model and store the answer as an annotation."""
from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
import subprocess

from .store import SUMMARIZER_MARKER, annotate, get_messages
from .util import cap, human_text

DEFAULT_COMMAND = "claude -p --model haiku"
WHITESPACE = re.compile(r"\s+")

PROMPT = SUMMARIZER_MARKER + """ so it can be found later.
You get the opening messages (the goal) and the latest messages (where it ended up). Describe the latest state:
what exists or was decided by the end, and what is still open. Name concrete files, systems, PRs and errors.
Return ONLY a JSON object: {"title": "<=8 word specific title", "summary": "2-3 sentences, latest state first"}
Transcript (user/assistant text only, tool calls omitted):
"""

# What the model reads: the opening exchange and the end of the conversation, nothing in between.
HEAD_MESSAGES, TAIL_MESSAGES = 4, 6
USER_CHARS, ASSISTANT_CHARS = 800, 500


def summarizer_command(flag: str | None = None) -> str:
    """--cmd-line, else $HIST_SUMMARIZER, else summarize.command in config.toml, else the default."""
    from .settings import settings
    return flag or os.environ.get("HIST_SUMMARIZER") or settings()["summarize"]["command"] or DEFAULT_COMMAND


def summarize_session(db: sqlite3.Connection, session_id: str, command: str = DEFAULT_COMMAND) -> dict:
    """Run the model on one session, store its title/summary, and return them. Raises RuntimeError on failure."""
    result = subprocess.run(shlex.split(command), input=PROMPT + condensed_transcript(db, session_id),
                            capture_output=True, text=True, timeout=180)
    answer = re.search(r"\{.*\}", result.stdout, re.S)
    if not answer:
        detail = (result.stderr or result.stdout or "no output").strip()[:200]
        raise RuntimeError(f"`{command}` exited {result.returncode} without JSON: {detail}")
    try:
        labels = json.loads(answer.group(0))
    except ValueError as e:
        raise RuntimeError(f"could not parse model output as JSON: {e}")
    annotate(db, session_id, title=labels.get("title"), summary=labels.get("summary"), by="llm")
    return labels


def condensed_transcript(db: sqlite3.Connection, session_id: str) -> str:
    """The first few and last few user/assistant messages, one capped line each; tool calls and injected
    preambles are left out, so the prompt stays small whatever the session's length."""
    lines = []
    for _, _, role, text in get_messages(db, session_id, include_tools=False):
        text = human_text(text) if role == "user" else text
        if text.strip():
            limit = USER_CHARS if role == "user" else ASSISTANT_CHARS
            lines.append(role.upper() + ": " + cap(WHITESPACE.sub(" ", text).strip(), limit))
    if len(lines) <= HEAD_MESSAGES + TAIL_MESSAGES:
        return "\n".join(lines)
    omitted = len(lines) - HEAD_MESSAGES - TAIL_MESSAGES
    return "\n".join(lines[:HEAD_MESSAGES] + [f"… ({omitted} messages omitted) …"] + lines[-TAIL_MESSAGES:])
