# hist — search all local AI agent sessions

One stdlib-only Python script (`./hist`) that indexes every local agent conversation into
`~/.cache/agent-history/index.db` (SQLite) and lets humans (TUI) and agents (CLI + JSON) search it.

| source           | where it reads from |
|------------------|---------------------|
| `claude`         | `~/.claude/projects/**/*.jsonl` (Claude Code CLI, IDE, desktop Code tab, teleported web sessions) |
| `claude-desktop` | `~/Library/Application Support/Claude/local-agent-mode-sessions/**` (Cowork) + desktop session titles |
| `codex`          | `~/.codex/sessions`, `~/.codex/archived_sessions` (Codex CLI, ChatGPT/Codex desktop app, IDE ext) + thread titles from `state_5.sqlite` |
| `cursor`         | Cursor `state.vscdb` composers/bubbles (IDE agent + background agents), `~/.cursor/projects/*/agent-transcripts` |

Each query does a quick incremental re-index (~0.1–0.3s); `--no-index` skips it, `hist index --full` rebuilds.

## For agents

```sh
hist search -q "rate limit" -d ~/src/app --since 7d --json   # JSONL, one session per line, matching messages in hits_preview
hist search -e 'Shadow (AI|skills)' --until 2026-09-15 -s codex,cursor --json
hist show <id-or-prefix> --no-tools --json                     # full transcript (+ --range 40:80, --tail N, --max-chars N)
hist show <id> --grep 'migration'                              # only matching messages
hist annotate <id> --title "..." --summary "..." --tags a,b --by my-agent   # persists across re-index
hist summarize -d ~/src/app --since 30d -n 20                  # LLM-fill missing summaries (claude -p, on demand)
hist dirs --json                                               # discover roots for -d
hist sources --json
hist help | hist <command> --help                              # every command documents flags, examples, JSON fields
```

Errors: exit 2 = bad arguments, 3 = session not found/ambiguous (the hint lists candidates). With `--json`,
errors are `{"error", "hint", "code"}` on stdout; progress and notes always go to stderr.

- Message-level filters (`-q`, `-e`, `--since`, `--until`, `--role`) must hold on the **same message**, so `--since 2d -q foo`
  finds old conversations where *foo* was said in the last two days.
- `-d` matches the session's working directory or anything beneath it.
- Default search covers user + assistant text; `--tools` adds tool calls/outputs (truncated to 2k chars).
- Subagent sessions are hidden unless `--subagents`.
- Each result has `title`, `summary` (`summary_source`: annotation > native > first_prompt), `first_prompt`,
  `last_prompt`, `resume` (shell command to reopen it), and `path` (raw transcript).

## TUI

`hist` (or `hist -d . --since 7d` to pre-filter). Opens fzf-style in the search box: results and the preview
pane update as you type; `↑↓` select while typing, `enter` opens the transcript at the first match (`n`/`N` next/prev
match), `tab` drops to the list. In the list: `/` text, `r` regex, `d` dir (`.` = cwd), `s`/`u` since/until,
`x` source, `t` include tools, `a` subagents, `c` clear, `R` re-index, `y` copy resume command, `q` quit.

## Not covered
- Plain ChatGPT chats and claude.ai chats live server-side, not on disk.
- Cursor background-agent details cached as protobuf (`bcCachedDetails`) aren't decoded; their conversations show
  up through the regular composer records when synced.

## Code layout

Python 3.9+ standard library only. `./hist` is a small launcher (safe to symlink onto PATH); the code is in `agenthist/`:

| module | what it does |
|---|---|
| `cli.py` | argument parsing, the subcommands, error reporting / exit codes |
| `tui.py` | curses UI: filter bar, live search box (`LineEditor`), result list, `TranscriptView` |
| `query.py` | `Query` (validated filters → SQL), `search`, directory and source listings |
| `store.py` | SQLite schema, `Session` record, writing/reading sessions, annotations |
| `sources/` | one parser per client (`claude.py`, `codex.py`, `cursor.py`) plus the incremental indexing driver |
| `summarize.py` | on-demand LLM titles/summaries |
| `util.py` | errors, timestamps, text cleanup, display formatting |

To support another client, add `sources/<name>.py` with `index(db, paths, full) -> int` that builds `store.Session`
objects and saves them (file-based clients can reuse `index_files`), then register it in `sources/__init__.py`.
