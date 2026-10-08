"""hist — search every local AI agent session (Claude Code, Claude desktop/Cowork, Codex, Cursor).

Start here:   hist help            (this text)      hist <command> --help   (flags + examples)
Humans:       hist                 (TUI)
Agents:       always pass --json; stdout is JSON, progress/notes go to stderr.

Commands:
  search      find sessions by text/regex, directory, message time, source
  show        print one transcript (paginate with --range / --tail, trim with --max-chars)
  dirs        list working directories that have sessions (discover values for --dir)
  sources     what is indexed per client
  annotate    attach your own title/summary/tags to a session (persists across re-index)
  summarize   LLM-fill missing titles/summaries on demand (default: claude -p --model haiku)
  index       refresh the index now (search/show/dirs already refresh incrementally, ~0.2s)
  ready       merge-readiness scan of the current branch (scope, narrating comments, new tests, debug, CI command)
  install-hooks  wire the merge-ready gate into Claude Code, Codex and Cursor (hook = the entry point they call)

Typical agent flow:
  hist dirs --json                                              # which roots exist
  hist search -d ~/src/app -q "rate limit" --since 7d --json    # candidate sessions + matching snippets
  hist show <id> --grep "rate limit" --json                     # just the relevant messages
  hist show <id> --range 40:80 --max-chars 2000 --json          # read around them

Exit codes: 0 ok (an empty result is still 0)  2 invalid arguments  3 session not found/ambiguous  1 internal error
Errors with --json: {"error": "...", "hint": "...", "code": N} on stdout.
Env: HIST_DB (index path, default ~/.cache/agent-history/index.db), HIST_AGENT (default --by), HIST_SUMMARIZER.
"""
