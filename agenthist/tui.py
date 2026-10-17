"""Interactive terminal UI.

Layout:  filter bar · result list · preview of the selected session · key help / input line.
The text filter is live: results update as you type (debounced), and ↑↓ move the selection meanwhile.
"""
from __future__ import annotations

import curses
import os
import re
import subprocess
import textwrap
import time
from dataclasses import dataclass

from . import markdown, store
from .query import Query, dir_warning, resume_argv, search
from .sources import index_all
from .util import local, short_path


@dataclass(frozen=True)
class Filter:
    key: str     # name in the args dict passed to Query.from_args
    label: str
    hotkey: str
    hint: str


FILTERS = [
    Filter("query", "Text", "/", "live · ↑↓ select · enter open · tab list · esc clear"),
    Filter("regex", "Regex", "r", "python/rg regex"),
    Filter("dir", "Dir", "d", "path; . = cwd"),
    Filter("since", "Since", "s", "2026-10-01, 3d, 12h, today"),
    Filter("until", "Until", "u", "date or 2d"),
    Filter("source", "Source", "x", "claude,claude-desktop,codex,cursor"),
]
FILTER_BY_HOTKEY = {f.hotkey: f for f in FILTERS}
LIVE_FILTERS = ("query", "regex")  # filters that search on every keystroke

SOURCE_BADGES = {"claude": "CC", "claude-desktop": "CD", "codex": "CX", "cursor": "CU"}
RESULT_LIMIT = 300
DEBOUNCE_MS = 90

HELP = ("/ live search · R reindex · ↑↓ move · enter open · r regex · d dir · s since · u until · x source · "
        "t tools · a subagents · c clear · y copy resume · p copy path · q quit")

# color pairs
CYAN, YELLOW, GREEN, MAGENTA, RED = 1, 2, 3, 4, 5


class TUI:
    def __init__(self, db, args: dict):
        self.db = db
        self.values = {}
        for f in FILTERS:
            value = args.get(f.key)
            self.values[f.key] = ",".join(value) if isinstance(value, list) else (value or "")
        self.include_tools = bool(args.get("tools"))
        self.include_subagents = False

        self.results: list[dict] = []
        self.selected = 0
        self.scroll = 0
        self.status = ""
        self.error = ""
        self.launch: tuple[list[str], str | None] | None = None  # set to quit and resume a session in its client

    # ---------------------------------------------------------------- state

    def current_query(self) -> Query:
        args = dict(self.values, tools=self.include_tools, subagents=self.include_subagents, limit=RESULT_LIMIT)
        if args["source"]:
            args["source"] = [args["source"]]
        return Query.from_args(args)

    def refresh(self, keep_selection: bool = False) -> None:
        previous_id = self.selected_session()["id"] if keep_selection and self.selected_session() else None
        started = time.time()
        try:
            query = self.current_query()
            self.results = search(self.db, query)
            self.error = "" if self.results else (dir_warning(self.db, query) or "")
            self.status = f"{len(self.results)} sessions · {time.time() - started:.2f}s"
        except Exception as e:  # invalid filter while typing: show it, keep going
            self.results, self.error = [], str(e)

        self.selected, self.scroll = 0, 0
        ids = [r["id"] for r in self.results]
        if previous_id in ids:
            self.selected = ids.index(previous_id)

    def selected_session(self) -> dict | None:
        return self.results[self.selected] if 0 <= self.selected < len(self.results) else None

    def move_selection(self, delta: int) -> None:
        self.selected = max(0, min(self.selected + delta, len(self.results) - 1))

    def set_filter(self, key: str, value: str) -> None:
        if key == "dir" and value == ".":
            value = os.getcwd()
        if key in ("since", "until") and is_partial_time(value):
            return  # e.g. the "3" of "3d": wait for the unit instead of flashing an error
        self.values[key] = value
        self.refresh(keep_selection=True)

    # ---------------------------------------------------------------- main loop

    def run(self, screen) -> tuple[list[str], str | None] | None:
        """Returns the (argv, cwd) of a session to resume, if one was chosen."""
        self.screen = screen
        setup_colors()
        curses.curs_set(0)
        self.refresh()
        self.edit_filter(FILTERS[0])  # start in the search box, like fzf

        while True:
            if self.launch:
                return self.launch
            self.draw()
            key = screen.get_wch()
            session = self.selected_session()

            if key in ("q", "\x1b"):
                return None
            elif key in FILTER_BY_HOTKEY:
                self.edit_filter(FILTER_BY_HOTKEY[key])
            elif key in (curses.KEY_DOWN, "j"):
                self.move_selection(1)
            elif key in (curses.KEY_UP, "k"):
                self.move_selection(-1)
            elif key == curses.KEY_NPAGE:
                self.move_selection(10)
            elif key == curses.KEY_PPAGE:
                self.move_selection(-10)
            elif key in ("\n", curses.KEY_ENTER, "l", curses.KEY_RIGHT) and session:
                self.open(session)
            elif key == "t":
                self.include_tools = not self.include_tools
                self.refresh()
            elif key == "a":
                self.include_subagents = not self.include_subagents
                self.refresh()
            elif key == "c":
                self.values = {f.key: "" for f in FILTERS}
                self.refresh()
            elif key == "R":
                self.status = "re-indexing…"
                self.draw()
                index_all(self.db, quiet=True)
                self.refresh(keep_selection=True)
            elif key == "y" and session:
                self.copy(session["resume"] or session["path"])
            elif key == "p" and session:
                self.copy(session["path"])

    def open(self, session: dict) -> None:
        query = self.current_query()
        TranscriptView(self, session, query.hit_pattern()).run()

    def copy(self, text: str) -> None:
        try:
            subprocess.run(["pbcopy"], input=text.encode(), check=True)
            self.status = f"copied: {text}"
        except (OSError, subprocess.CalledProcessError) as e:
            self.status = f"copy failed: {e}"

    def edit_filter(self, f: Filter) -> None:
        """Edit one filter in the bottom line. Live filters search after each pause in typing."""
        editor = LineEditor(self.values[f.key])
        original = self.values[f.key]
        live = f.key in LIVE_FILTERS
        pending = False

        curses.curs_set(1)
        self.screen.timeout(DEBOUNCE_MS)
        while True:
            self.draw(show_help=False)
            self.draw_input(f"{f.label} ({f.hint}): ", editor)
            try:
                key = self.screen.get_wch()
            except curses.error:  # no key within the debounce window
                if pending and live:
                    self.set_filter(f.key, editor.text)
                    pending = False
                continue

            if key in ("\n", curses.KEY_ENTER):
                open_after = live and bool(editor.text)
                break
            if key == "\t":
                open_after = False
                break
            if key == "\x1b":
                # esc on a live filter clears it, or leaves if already empty; on other filters it cancels
                if live and editor.text:
                    editor.set("")
                    pending = True
                    continue
                if not live:
                    editor.set(original)
                open_after = False
                break
            if key in (curses.KEY_DOWN, "\x0e"):     # ↓ / ctrl-n
                self.move_selection(1)
            elif key in (curses.KEY_UP, "\x10"):     # ↑ / ctrl-p
                self.move_selection(-1)
            elif key == curses.KEY_NPAGE:
                self.move_selection(10)
            elif key == curses.KEY_PPAGE:
                self.move_selection(-10)
            elif editor.handle(key):
                pending = True

        self.screen.timeout(-1)
        curses.curs_set(0)
        if editor.text != self.values[f.key] or f.key == "dir":
            self.set_filter(f.key, editor.text)
        if open_after and self.selected_session() and not self.error:
            self.open(self.selected_session())

    # ---------------------------------------------------------------- drawing

    def put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        """Write text clipped to the screen; never raises on the bottom-right cell."""
        height, width = self.screen.getmaxyx()
        if 0 <= y < height and x < width:
            try:
                self.screen.addnstr(y, x, text.replace("\n", " "), width - x - 1, attr)
            except curses.error:
                pass

    def draw(self, show_help: bool = True) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        list_height = max(3, (height - 4) // 2)

        self.draw_filter_bar(width)
        self.draw_results(top=2, height=list_height, width=width)
        self.put(2 + list_height, 0, "─" * (width - 1), curses.A_DIM)
        self.draw_preview(top=3 + list_height, bottom=height - 1, width=width)
        if show_help:
            self.put(height - 1, 0, HELP, curses.A_DIM)
        self.screen.refresh()

    def draw_filter_bar(self, width: int) -> None:
        x = 0
        for f in FILTERS:
            label = f"{f.label}[{f.hotkey}]: "
            value = self.values[f.key]
            self.put(0, x, label, curses.color_pair(CYAN))
            self.put(0, x + len(label), value or "·", curses.A_BOLD if value else curses.A_DIM)
            x += len(label) + len(value or "·") + 2

        toggles = (f"tools[t]:{'on' if self.include_tools else 'off'}  "
                   f"subagents[a]:{'on' if self.include_subagents else 'off'}  clear[c]")
        self.put(1, 0, toggles, curses.A_DIM)
        message = self.error or self.status
        self.put(1, max(0, width - len(message) - 2), message, curses.color_pair(RED) if self.error else curses.A_DIM)

    def draw_results(self, top: int, height: int, width: int) -> None:
        # keep the selection in view
        if self.selected < self.scroll:
            self.scroll = self.selected
        if self.selected >= self.scroll + height:
            self.scroll = self.selected - height + 1

        for row, session in enumerate(self.results[self.scroll:self.scroll + height]):
            index = self.scroll + row
            badge = SOURCE_BADGES.get(session["source"], "??")
            hits = f"{session['hits']:>4}" if session["hits"] else "    "
            title = (session["title"] or "(untitled)")[:60]
            line = f"{local(session['last_hit'])} {badge} {hits} {title:<60} {short_path(session['cwd'])}"
            self.put(top + row, 0, line.ljust(width), curses.A_REVERSE if index == self.selected else 0)

    def draw_preview(self, top: int, bottom: int, width: int) -> None:
        session = self.selected_session()
        if not session:
            return
        y = top
        self.put(y, 0, session["title"] or "(untitled)", curses.A_BOLD)
        details = (f"{session['source']}  {short_path(session['cwd'])}  {session['branch'] or ''}  "
                   f"{local(session['started'])} → {local(session['updated'])}  {session['n_msgs']} msgs")
        self.put(y + 1, 0, details, curses.color_pair(CYAN))
        self.put(y + 2, 0, f"resume: {session['resume'] or '-'}", curses.A_DIM)
        y += 3

        for line in textwrap.wrap(session["summary"] or "", width - 2)[:3]:
            self.put(y, 0, line, curses.color_pair(GREEN))
            y += 1

        hits = session.get("hits_preview") or []
        if not hits and session["last_prompt"] and session["last_prompt"] != session["first_prompt"]:
            self.put(y, 0, "last prompt: " + session["last_prompt"], curses.A_DIM)
            y += 1

        for hit in hits:
            if y >= bottom:
                break
            self.put(y, 0, f"{local(hit['ts'])} {hit['role']}", curses.color_pair(YELLOW))
            for line in textwrap.wrap(hit["snippet"], width - 4)[:3]:
                y += 1
                self.put(y, 2, line)
            y += 1

    def draw_input(self, prompt: str, editor: "LineEditor") -> None:
        height, width = self.screen.getmaxyx()
        self.put(height - 1, 0, " " * (width - 1))
        self.put(height - 1, 0, prompt, curses.color_pair(CYAN))
        self.put(height - 1, len(prompt), editor.text, curses.A_BOLD)
        self.screen.move(height - 1, min(width - 2, len(prompt) + editor.cursor))


class LineEditor:
    """A single-line text buffer with readline-style keys (ctrl-a/e/u/w, arrows, home/end)."""

    def __init__(self, text: str = ""):
        self.set(text)

    def set(self, text: str) -> None:
        self.text, self.cursor = text, len(text)

    def handle(self, key) -> bool:
        """Apply a key. Returns True if the text changed."""
        before, text, cursor = self.text, self.text, self.cursor
        if key == curses.KEY_LEFT:
            cursor = max(0, cursor - 1)
        elif key == curses.KEY_RIGHT:
            cursor = min(len(text), cursor + 1)
        elif key in ("\x01", curses.KEY_HOME):
            cursor = 0
        elif key in ("\x05", curses.KEY_END):
            cursor = len(text)
        elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            if cursor:
                text, cursor = text[:cursor - 1] + text[cursor:], cursor - 1
        elif key == "\x15":  # ctrl-u: delete to start
            text, cursor = text[cursor:], 0
        elif key == "\x17":  # ctrl-w: delete previous word
            kept = re.sub(r"\S*\s*$", "", text[:cursor])
            text, cursor = kept + text[cursor:], len(kept)
        elif isinstance(key, str) and key.isprintable():
            text, cursor = text[:cursor] + key + text[cursor:], cursor + 1
        self.text, self.cursor = text, cursor
        return text != before


class TranscriptView:
    """Scrollable transcript of one session, opened at the first match with matches highlighted."""

    def __init__(self, tui: TUI, session: dict, hit_pattern: re.Pattern | None):
        self.tui, self.session, self.hit_pattern = tui, session, hit_pattern
        messages = store.get_messages(tui.db, session["id"], include_tools=tui.include_tools)
        _, width = tui.screen.getmaxyx()
        self.lines, self.match_lines = self.layout(messages, width)
        self.top = self.match_lines[0] if self.match_lines else 0

    def layout(self, messages: list[tuple], width: int) -> tuple[list[tuple], list[int]]:
        """[(spans, highlight?)] wrapped to width, plus the line numbers of matching messages' headers.

        spans is [(text, attr)]; user and assistant messages are rendered as Markdown, tool output as plain text.
        """
        role_colors = {"user": GREEN, "assistant": CYAN, "tool": MAGENTA}
        lines, match_lines = [], []
        for _, ts, role, text in messages:
            is_match = bool(self.hit_pattern and self.hit_pattern.search(text))
            if is_match:
                match_lines.append(len(lines))
            header = f"── {role} · {local(ts)}" + ("  ◀ match" if is_match else "")
            lines.append(([(header, curses.color_pair(role_colors.get(role, 0)) | curses.A_BOLD)], False))

            if role == "tool":
                for paragraph in text[:800].split("\n"):
                    for line in textwrap.wrap(paragraph, width - 2) or [""]:
                        lines.append(([(line, 0)], True))
            else:
                for line in markdown.render(text, width - 2):
                    lines.append(([(t, style_attr(styles)) for t, styles in line if t], True))
            lines.append(([], False))
        return lines, match_lines

    def run(self) -> None:
        screen = self.tui.screen
        while True:
            self.draw()
            height, _ = screen.getmaxyx()
            page = height - 3
            last = max(0, len(self.lines) - 1)
            key = screen.get_wch()

            if key in ("q", "\x1b", "h", curses.KEY_LEFT):
                return
            elif key in (curses.KEY_DOWN, "j"):
                self.top = min(self.top + 1, last)
            elif key in (curses.KEY_UP, "k"):
                self.top = max(self.top - 1, 0)
            elif key in (curses.KEY_NPAGE, " "):
                self.top = min(self.top + page, last)
            elif key == curses.KEY_PPAGE:
                self.top = max(self.top - page, 0)
            elif key == "g":
                self.top = 0
            elif key == "G":
                self.top = max(0, len(self.lines) - height + 2)
            elif key == "n":
                later = [i for i in self.match_lines if i > self.top]
                self.top = later[0] if later else self.top
            elif key == "N":
                earlier = [i for i in self.match_lines if i < self.top]
                self.top = earlier[-1] if earlier else self.top
            elif key in ("\n", curses.KEY_ENTER):
                resume = resume_argv(self.session)
                if resume:
                    self.tui.launch = resume
                    return
                self.tui.status = f"can't resume {self.session['source']} sessions"
            elif key == "y":
                self.tui.copy(self.session["resume"] or self.session["path"])

    def draw(self) -> None:
        tui, screen = self.tui, self.tui.screen
        screen.erase()
        height, _ = screen.getmaxyx()
        tui.put(0, 0, f"{self.session['title'] or '(untitled)'} — {short_path(self.session['cwd'])}",
                curses.A_BOLD | curses.A_REVERSE)

        for row, (spans, highlight) in enumerate(self.lines[self.top:self.top + height - 2]):
            x = 0
            for text, attr in spans:
                tui.put(1 + row, x, text, attr)
                x += len(text)
            if highlight and self.hit_pattern:
                text = "".join(t for t, _ in spans)
                for match in self.hit_pattern.finditer(text):
                    if match.end() > match.start():
                        tui.put(1 + row, match.start(), match.group(0), curses.color_pair(YELLOW) | curses.A_REVERSE)

        footer = f"{self.top + 1}/{len(self.lines)}  ↑↓ pgup/pgdn g/G · n/N next/prev match · enter resume · y copy resume · q back"
        tui.put(height - 1, 0, footer, curses.A_DIM)
        screen.refresh()


def avoid_broken_rep() -> None:
    """Old ncurses (macOS ships 6.0) garbles the terminal's repeat-character command for multibyte characters,
    so runs like ──── in table borders vanish and stale screen content shows through. Fall back to a terminal
    type without that command."""
    if curses.ncurses_version >= (6, 1):
        return
    try:
        curses.setupterm()
        if curses.tigetstr("rep"):
            os.environ["TERM"] = "xterm-256color"
    except curses.error:
        pass


def setup_colors() -> None:
    curses.use_default_colors()
    for pair, color in ((CYAN, curses.COLOR_CYAN), (YELLOW, curses.COLOR_YELLOW), (GREEN, curses.COLOR_GREEN),
                        (MAGENTA, curses.COLOR_MAGENTA), (RED, curses.COLOR_RED)):
        curses.init_pair(pair, color, -1)


def style_attr(styles: frozenset) -> int:
    """Curses attributes for a set of markdown span styles."""
    attr = 0
    if "bold" in styles:
        attr |= curses.A_BOLD
    if "italic" in styles:
        attr |= getattr(curses, "A_ITALIC", curses.A_UNDERLINE)
    if "code" in styles:
        attr |= curses.color_pair(YELLOW)
    if "link" in styles:
        attr |= curses.color_pair(CYAN) | curses.A_UNDERLINE
    if "heading" in styles:
        attr |= curses.color_pair(MAGENTA) | curses.A_BOLD
    if "dim" in styles:
        attr |= curses.A_DIM
    return attr


def is_partial_time(value: str) -> bool:
    """True for a relative time still being typed ("3" before "3d"), but not for ISO dates."""
    return bool(value) and value[-1].isdigit() and not re.fullmatch(r"\d{4}-\d\d-\d\d.*", value)
