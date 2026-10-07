"""On-demand LLM titles/summaries: send a condensed transcript to a CLI model and store the answer as an annotation."""
from __future__ import annotations

import json
import re
import shlex
import sqlite3
import subprocess

from .store import SUMMARIZER_MARKER, annotate, get_messages

DEFAULT_COMMAND = "claude -p --model haiku"
WHITESPACE = re.compile(r"\s+")

PROMPT = SUMMARIZER_MARKER + """ so it can be found later.
Return ONLY a JSON object: {"title": "<=8 word specific title", "summary": "2-3 sentences: the goal, what was done/decided, outcome or open state. Name concrete files, systems, PRs, errors."}
Transcript (user/assistant text only, truncated):
"""


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


def condensed_transcript(db: sqlite3.Connection, session_id: str, budget: int = 14_000) -> str:
    """User and assistant text, one line per message. Long sessions keep their beginning and end,
    which carry the goal and the outcome, and drop the middle."""
    lines = []
    for _, _, role, text in get_messages(db, session_id, include_tools=False):
        limit = 1500 if role == "user" else 600
        lines.append(role.upper() + ": " + WHITESPACE.sub(" ", text)[:limit])

    if sum(len(line) for line in lines) <= budget:
        return "\n".join(lines)

    # Alternate taking lines from the front and the back until the budget runs out.
    head, tail, used = [], [], 0
    front, back = 0, len(lines) - 1
    take_front = True
    while front <= back:
        line = lines[front] if take_front else lines[back]
        if used + len(line) > budget:
            break
        used += len(line)
        if take_front:
            head.append(line)
            front += 1
        else:
            tail.append(line)
            back -= 1
        take_front = not take_front
    return "\n".join(head + ["…"] + tail[::-1])
