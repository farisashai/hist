"""Command-line interface: argument parsing, the subcommands, and error reporting."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

from . import __doc__ as OVERVIEW
from . import store
from .query import Query, dir_warning, list_dirs, parse_sources, parse_when, resume_command, search, source_stats
from .sources import SOURCES, index_all
from .summarize import DEFAULT_COMMAND, summarize_session
from .util import HistError, cap, local, note, short_path

BOLD, DIM, CYAN, RESET = "\033[1m", "\033[2m", "\033[36m", "\033[0m"


# ---------------------------------------------------------------- commands

def cmd_search(db, args: dict) -> None:
    q = Query.from_args(args)
    warning = dir_warning(db, q)
    if warning:
        note(warning)
    results = search(db, q)

    if args["json"]:
        for session in results:
            print_json(session)
        return
    if not results:
        note("no matches")
    for s in results:
        hits = f", {s['hits']} hits" if s["hits"] else ""
        print(f"{BOLD}{local(s['last_hit'])}  {s['source']:<14} {s['title'] or '(untitled)'}{RESET}")
        print(f"  {short_path(s['cwd'])}   id={s['native_id']}   {s['n_msgs']} msgs{hits}")
        print(f"  {CYAN}{(s['summary'] or '')[:200]}{RESET}")
        for hit in s.get("hits_preview", []):
            print(f"    {DIM}{local(hit['ts'])} {hit['role']:<9}{RESET} {hit['snippet'][:160]}")


def cmd_show(db, args: dict) -> None:
    session_id = store.resolve_session(db, args["session"])
    session = store.get_session(db, session_id)
    messages = store.get_messages(db, session_id, include_tools=not args["no_tools"])
    total = len(messages)
    messages = filter_messages(messages, args)

    if args["json"]:
        session["resume"] = resume_command(session)
        session["total_messages"] = total
        session["messages"] = [dict(zip(["seq", "ts", "role", "text"], m)) for m in messages]
        print_json(session)
        return

    print(f"# {session['title'] or '(untitled)'}")
    print(session["summary"] or "")
    print(f"{session['source']}  {short_path(session['cwd'])}  {local(session['started'])} → {local(session['updated'])}")
    print(session["path"])
    print(f"resume: {resume_command(session)}\n")
    for seq, ts, role, text in messages:
        print(f"── [{seq}] {role} · {local(ts)}")
        print(text if role != "tool" else text[:600])
        print()


def filter_messages(messages: list[tuple], args: dict) -> list[tuple]:
    """Apply show's --grep, --range, --tail and --max-chars, in that order."""
    if args.get("grep"):
        try:
            pattern = re.compile(args["grep"], re.I)
        except re.error as e:
            raise HistError(f"invalid --grep {args['grep']!r}: {e}")
        messages = [m for m in messages if pattern.search(m[3])]

    if args.get("range"):
        bounds = re.fullmatch(r"(\d*):(\d*)", args["range"])
        if not bounds:
            raise HistError(f"invalid --range {args['range']!r}", "START:END over message seq numbers, e.g. 40:80, :20, 100:")
        low = int(bounds.group(1) or 0)
        high = int(bounds.group(2)) if bounds.group(2) else float("inf")
        messages = [m for m in messages if low <= m[0] < high]

    if args.get("tail"):
        messages = messages[-args["tail"]:]

    if args.get("max_chars"):
        messages = [(seq, ts, role, cap(text, args["max_chars"])) for seq, ts, role, text in messages]
    return messages


def cmd_dirs(db, args: dict) -> None:
    if args["limit"] < 1:
        raise HistError("--limit must be >= 1")
    rows = list_dirs(db, parse_sources(args.get("source")), parse_when(args.get("since")), args["limit"])
    if args["json"]:
        for row in rows:
            print(json.dumps(row))
        return
    for row in rows:
        print(f"{local(row['last_active'])}  {row['sessions']:>4}  {short_path(row['cwd']):<70} {row['sources']}")


def cmd_sources(db, args: dict) -> None:
    stats = source_stats(db)
    if args.get("json"):
        print(json.dumps(stats))
        return
    for s in stats:
        print(f"{s['source']:<15} {s['sessions']:>6} sessions {s['messages']:>8} msgs   {local(s['first'])} → {local(s['last'])}")


def cmd_annotate(db, args: dict) -> None:
    session_id = store.resolve_session(db, args["session"])
    if args["clear"]:
        store.clear_annotation(db, session_id)
    else:
        store.annotate(db, session_id, args["title"], args["summary"], args["tags"], args["by"])
    print_json(store.get_session(db, session_id))


def cmd_summarize(db, args: dict) -> None:
    if args.get("session"):
        targets = [store.get_session(db, store.resolve_session(db, args["session"]))]
    else:
        args["missing_summary"] = not args["force"]
        targets = search(db, Query.from_args(args))
    if not targets:
        note("nothing to summarize (all matching sessions already have summaries; --force to redo)")
        return

    print(f"summarizing {len(targets)} session(s) with `{args['cmd_line']}` …", file=sys.stderr)
    for session in targets:
        try:
            labels = summarize_session(db, session["id"], args["cmd_line"])
        except (RuntimeError, OSError, ValueError) as e:
            print(f"✗ {session['native_id']}: {e}", file=sys.stderr)
            continue
        print(f"✓ {session['native_id']}  {labels.get('title')}", file=sys.stderr)
        if args["json"]:
            print_json(store.get_session(db, session["id"]))


def cmd_index(db, args: dict) -> None:
    index_all(db, full=args["full"])
    cmd_sources(db, {"json": False})


def cmd_tui(db, args: dict) -> None:
    import curses
    from .tui import TUI

    os.environ.setdefault("ESCDELAY", "25")  # make esc respond immediately
    curses.wrapper(TUI(db, args).run)


COMMANDS = {
    "search": cmd_search,
    "show": cmd_show,
    "dirs": cmd_dirs,
    "sources": cmd_sources,
    "annotate": cmd_annotate,
    "summarize": cmd_summarize,
    "index": cmd_index,
    "tui": cmd_tui,
}


def print_json(value) -> None:
    print(json.dumps(value, ensure_ascii=False))


# ---------------------------------------------------------------- argument parser

class Parser(argparse.ArgumentParser):
    """argparse, but usage errors go through the same error reporting as everything else."""

    def error(self, message):
        raise HistError(message, f"see `{self.prog} --help`")


def build_parser() -> tuple[Parser, argparse._SubParsersAction]:
    formatter = argparse.RawDescriptionHelpFormatter
    parser = Parser(prog="hist", description=OVERVIEW, formatter_class=formatter)
    commands = parser.add_subparsers(dest="cmd", metavar="COMMAND", parser_class=Parser)

    def command(name: str, summary: str, examples: list[str]) -> Parser:
        epilog = "examples:\n" + "\n".join("  " + e for e in examples)
        return commands.add_parser(name, help=summary, description=summary, formatter_class=formatter, epilog=epilog)

    add_search_flags(command("search", "find sessions; newest matching activity first", [
        'hist search -q "connection refused" --json',
        'hist search -d ~/src/app --since 2026-10-01 --until 2026-10-03 --json',
        'hist search -e "TODO\\(\\w+\\)" -s claude,codex --role user --json',
        'hist search --since 4h --json   # sessions with any message in the last 4 hours',
        "JSON fields: id native_id source title summary summary_source first_prompt last_prompt tags cwd branch",
        "             started updated n_msgs hits last_hit resume path hits_preview[{seq ts role snippet}]",
    ]))
    add_search_flags(command("tui", "interactive search (the default when run with no command)",
                             ["hist", "hist -d . --since 7d"]))

    show = command("show", "print one session's transcript", [
        "hist show <id> --json                     # id, native_id, or any unique prefix",
        'hist show claude:c40fd05c --grep "migration" --json',
        "hist show <id> --range 40:80 --max-chars 1500 --json",
        "hist show <id> --tail 10 --no-tools",
        "JSON: session fields + total_messages + messages[{seq ts role text}]",
    ])
    show.add_argument("session", help="id (source:native_id), native id, or unique prefix/substring")
    show.add_argument("--grep", metavar="RE", help="only messages matching RE (case-insensitive)")
    show.add_argument("--range", metavar="A:B", help="only messages with A <= seq < B")
    show.add_argument("--tail", type=int, metavar="N", help="only the last N messages (after other filters)")
    show.add_argument("--max-chars", type=int, metavar="N", help="truncate each message to N chars")
    show.add_argument("--no-tools", action="store_true", help="omit tool calls/outputs")
    show.add_argument("--json", action="store_true")
    show.add_argument("--no-index", action="store_true")

    dirs = command("dirs", "list working directories that have sessions, most recently active first", [
        "hist dirs --json", "hist dirs -s cursor --since 7d"])
    dirs.add_argument("-s", "--source", action="append", metavar="SRC")
    dirs.add_argument("--since", metavar="WHEN", help="only dirs with activity since WHEN")
    dirs.add_argument("-n", "--limit", type=int, default=100, metavar="N")
    dirs.add_argument("--json", action="store_true")
    dirs.add_argument("--no-index", action="store_true")

    annotate = command("annotate", "attach a title/summary/tags to a session; wins over native titles and survives re-index", [
        'hist annotate <id> --title "Bazel cache audit" --summary "..." --tags bazel,ci --by my-agent',
        "hist annotate <id> --clear"])
    annotate.add_argument("session")
    annotate.add_argument("--title")
    annotate.add_argument("--summary")
    annotate.add_argument("--tags", help="comma separated")
    annotate.add_argument("--by", default=os.environ.get("HIST_AGENT", "user"), help="author label (default $HIST_AGENT or 'user')")
    annotate.add_argument("--clear", action="store_true", help="remove the annotation")

    summarize = command("summarize", "LLM-generate title+summary for sessions lacking an annotation (runs only when asked)", [
        "hist summarize <id>", "hist summarize -d ~/src/app --since 14d -n 20",
        'HIST_SUMMARIZER="codex exec" hist summarize -s cursor'])
    summarize.add_argument("session", nargs="?", help="one session; otherwise the filters select up to -n sessions")
    add_search_flags(summarize, default_limit=10)
    summarize.add_argument("--force", action="store_true", help="also redo sessions that already have a summary")
    summarize.add_argument("--cmd-line", default=os.environ.get("HIST_SUMMARIZER", DEFAULT_COMMAND),
                           help=f"command reading the prompt on stdin, printing JSON (default: $HIST_SUMMARIZER or {DEFAULT_COMMAND})")

    index = command("index", "refresh the index (incremental; --full re-parses everything)", ["hist index", "hist index --full"])
    index.add_argument("--full", action="store_true")

    sources = command("sources", "per-client counts and date ranges", ["hist sources --json"])
    sources.add_argument("--json", action="store_true")
    sources.add_argument("--no-index", action="store_true")

    command("help", "show this help", ["hist help", "hist help search"]).add_argument("topic", nargs="?")
    return parser, commands


def add_search_flags(parser: Parser, default_limit: int = 50) -> None:
    filters = parser.add_argument_group("filters (AND-ed; message filters must match the same message)")
    filters.add_argument("-q", "--query", metavar="TEXT", help="case-insensitive literal substring (like rg -F -i)")
    filters.add_argument("-e", "--regex", metavar="RE", help="regex, case-sensitive unless (?i) (like rg -e)")
    filters.add_argument("-d", "--dir", metavar="PATH", help="session cwd is PATH or below it; '.' = current dir")
    filters.add_argument("--since", metavar="WHEN", help="message sent at/after WHEN: 2026-10-01, 2026-10-01T14:00, 3d, 12h, today")
    filters.add_argument("--until", metavar="WHEN", help="message sent before WHEN (a bare date includes that whole day)")
    filters.add_argument("-s", "--source", action="append", metavar="SRC", help=f"{'|'.join(SOURCES)}; repeat or comma-separate")
    filters.add_argument("--role", action="append", choices=["user", "assistant", "tool"], help="restrict matching messages by role")
    filters.add_argument("--tools", action="store_true", help="also match tool calls/outputs (default: user+assistant text)")
    filters.add_argument("--subagents", action="store_true", help="include subagent sessions (hidden by default)")
    filters.add_argument("--missing-summary", action="store_true", help="only sessions without an annotation summary")

    output = parser.add_argument_group("output")
    output.add_argument("-n", "--limit", type=int, default=default_limit, metavar="N", help=f"max sessions (default {default_limit})")
    output.add_argument("--json", action="store_true", help="JSONL: one session object per line")
    output.add_argument("--no-index", action="store_true", help="skip the incremental refresh")


# ---------------------------------------------------------------- entry point

def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        run(argv)
    except HistError as e:
        exit_with_error(str(e), e.hint, e.code, as_json="--json" in argv)
    except BrokenPipeError:  # e.g. `hist search | head`
        pass
    except KeyboardInterrupt:
        sys.exit(130)


def run(argv: list[str]) -> None:
    if not argv:
        argv = ["tui"]
    elif argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        argv = ["tui"] + argv  # `hist -d . --since 7d` opens the TUI pre-filtered

    parser, commands = build_parser()
    args = vars(parser.parse_args(argv))
    command = args["cmd"]

    if command == "help":
        topic = commands.choices.get(args.get("topic"), parser)
        topic.print_help()
        return
    if command == "tui" and not sys.stdout.isatty():
        raise HistError("the TUI needs a terminal", "agents: use `hist search --json` / `hist show --json` (see `hist help`)")

    db = store.connect()
    index_is_empty = not db.execute("select 1 from sessions limit 1").fetchone()
    wants_refresh = command not in ("index", "annotate") and (not args.get("no_index") or index_is_empty)
    if wants_refresh:
        index_all(db, quiet=args.get("json") or not sys.stderr.isatty())

    COMMANDS[command](db, args)


def exit_with_error(message: str, hint: str | None, code: int, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"error": message, "hint": hint, "code": code}))
    else:
        print(f"hist: error: {message}" + (f"\nhint: {hint}" if hint else ""), file=sys.stderr)
    sys.exit(code)
