"""Render Markdown to styled, wrapped terminal lines.

Handles fenced code blocks, tables, headings, lists, block quotes, rules, and inline bold/italic/code/links.
A line is a list of (text, styles) spans; styles is a frozenset of names the caller maps to terminal attributes:
bold, italic, code, link, heading, dim.
"""
from __future__ import annotations

import os
import re

Span = tuple[str, frozenset]
Line = list[Span]

NONE = frozenset()
DIM = frozenset({"dim"})

INLINE = re.compile(
    r"(?P<ticks>`+)(?P<code>.+?)(?P=ticks)"
    r"|\[(?P<link_text>[^\]]+)\]\((?P<url>[^)\s]+)\)"
    r"|\*\*(?P<bold>.+?)\*\*"
    r"|(?<!\w)__(?P<bold2>.+?)__(?!\w)"
    r"|(?<![\w*])\*(?P<italic>[^*\s](?:[^*]*[^*\s])?)\*(?![\w*])"
    r"|(?<!\w)_(?P<italic2>[^_\s](?:[^_]*[^_\s])?)_(?!\w)"
)
FENCE = re.compile(r"^\s*(`{3,}|~{3,})\s*([\w+#.-]*)")
TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
LIST_ITEM = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(?:\[([ xX])\]\s+)?(.*)")
RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")


def render(text: str, width: int) -> list[Line]:
    width = max(width, 10)
    lines = text.split("\n")
    out: list[Line] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        fence = FENCE.match(line)
        if fence:
            marker, body = fence.group(1), []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(marker):
                body.append(lines[i])
                i += 1
            out += code_block(body, fence.group(2), width)
            i += 1  # closing fence
            continue

        if stripped.startswith("|") and i + 1 < len(lines) and TABLE_SEPARATOR.match(lines[i + 1]):
            rows = [line]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i])
                i += 1
            out += table([split_row(r) for r in rows], width)
            continue

        i += 1
        if not stripped:
            out.append([])
        elif RULE.match(line):
            out.append([("─" * min(width, 40), DIM)])
        elif heading := HEADING.match(stripped):
            out += wrap(inline(heading.group(2), frozenset({"heading"})), width)
        elif stripped.startswith(">"):
            bar = [("│ ", DIM)]
            out += wrap(inline(stripped.lstrip(">").strip(), frozenset({"italic"})), width, bar, bar)
        elif item := LIST_ITEM.match(line):
            indent, marker, check, rest = item.groups()
            bullet = marker if marker[0].isdigit() else "•"
            if check is not None:
                bullet = "☑" if check.lower() == "x" else "☐"
            prefix = " " * len(indent) + bullet + " "
            out += wrap(inline(rest), width, [(prefix, NONE)], [(" " * len(prefix), NONE)])
        else:
            indent = [(line[:len(line) - len(line.lstrip())], NONE)]
            out += wrap(inline(stripped), width, indent, indent)
    return out


def inline(text: str, styles: frozenset = NONE) -> list[Span]:
    spans: list[Span] = []
    pos = 0
    for m in INLINE.finditer(text):
        if m.start() > pos:
            spans.append((text[pos:m.start()], styles))
        if m.group("ticks"):
            spans.append((m.group("code").strip() or m.group("code"), styles | {"code"}))
        elif m.group("link_text") is not None:
            spans += inline(m.group("link_text"), styles | {"link"})
            target = link_target(m.group("link_text"), m.group("url"))
            if target:
                spans.append((f" ({target})", styles | {"dim"}))
        elif (bold := m.group("bold") or m.group("bold2")) is not None:
            spans += inline(bold, styles | {"bold"})
        else:
            spans += inline(m.group("italic") or m.group("italic2"), styles | {"italic"})
        pos = m.end()
    if pos < len(text):
        spans.append((text[pos:], styles))
    return spans


def link_target(text: str, url: str) -> str:
    """What to show after a link's text: the URL, or just the file name for local paths."""
    if url == text or url.startswith("#"):
        return ""
    if re.match(r"^[a-z][\w+.-]*://", url) or url.startswith("mailto:"):
        return url
    return os.path.basename(url.rstrip("/")) or url


def wrap(spans: list[Span], width: int, first: Line | None = None, rest: Line | None = None) -> list[Line]:
    """Word-wrap styled spans. `first`/`rest` are prefixes for the first and following lines."""
    first, rest = first or [], rest or []
    words: list[list[Span]] = []
    word: list[Span] = []
    for text, styles in spans:
        for piece in re.split(r"(\s+)", text):
            if not piece:
                continue
            if piece.isspace():
                if word:
                    words.append(word)
                word = []
            else:
                word.append((piece, styles))
    if word:
        words.append(word)

    lines: list[Line] = []
    line, col = list(first), length(first)
    available = lambda: width - length(first if not lines else rest)
    for word in words:
        size = length(word)
        has_text = col > length(first if not lines else rest)
        if has_text and col + 1 + size > width:
            lines.append(line)
            line, col, has_text = list(rest), length(rest), False
        if has_text:
            # the gap takes the styles both neighbours share, so underlined links stay continuous
            line.append((" ", line[-1][1] & word[0][1]))
            col += 1
        if size > available():
            for chunk in split_chars(word, width - col, available()):
                if col + length(chunk) > width:
                    lines.append(line)
                    line, col = list(rest), length(rest)
                line += chunk
                col += length(chunk)
        else:
            line += word
            col += size
    lines.append(line)
    return lines


def split_chars(spans: list[Span], first_size: int, size: int) -> list[list[Span]]:
    """Cut spans into chunks of at most `size` characters (the first chunk `first_size`)."""
    chunks, chunk, room = [], [], max(1, first_size)
    for text, styles in spans:
        while text:
            take = text[:room]
            chunk.append((take, styles))
            text, room = text[len(take):], room - len(take)
            if room == 0:
                chunks.append(chunk)
                chunk, room = [], max(1, size)
    if chunk:
        chunks.append(chunk)
    return chunks


def code_block(body: list[str], lang: str, width: int) -> list[Line]:
    out: list[Line] = [[("╭─ " + lang if lang else "╭─", DIM)]]
    code = frozenset({"code"})
    for text in body:
        text = text.expandtabs(4)
        for start in range(0, max(len(text), 1), max(1, width - 2)):
            out.append([("│ ", DIM), (text[start:start + width - 2], code)])
    out.append([("╰─", DIM)])
    return out


def split_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", line)]


def table(rows: list[list[str]], width: int) -> list[Line]:
    columns = max(len(r) for r in rows)
    cells = [[inline(c, frozenset({"bold"}) if r == 0 else NONE) for c in row + [""] * (columns - len(row))]
             for r, row in enumerate(rows)]
    natural = [max(max(length(cells[r][c]), 1) for r in range(len(cells))) for c in range(columns)]
    widths = fit_columns(natural, width - (3 * columns + 1))

    wrapped = [[wrap(cell, w) for cell, w in zip(row, widths)] for row in cells]
    multiline = any(len(cell) > 1 for row in wrapped for cell in row)

    def border(left: str, mid: str, right: str) -> Line:
        return [(left + mid.join("─" * (w + 2) for w in widths) + right, DIM)]

    out = [border("┌", "┬", "┐")]
    for r, row in enumerate(wrapped):
        if r == 1 or (r > 1 and multiline):
            out.append(border("├", "┼", "┤"))
        for k in range(max(len(cell) for cell in row)):
            line: Line = [("│", DIM)]
            for cell, w in zip(row, widths):
                content = cell[k] if k < len(cell) else []
                line += [(" ", NONE), *content, (" " * (w - length(content) + 1), NONE), ("│", DIM)]
            out.append(line)
    out.append(border("└", "┴", "┘"))
    return out


def fit_columns(natural: list[int], available: int) -> list[int]:
    """Column widths that fit `available`: narrow columns keep their size, wide ones share the rest."""
    if sum(natural) <= available:
        return natural
    widths = list(natural)
    remaining, left = max(available, 3 * len(natural)), len(natural)
    for c in sorted(range(len(natural)), key=natural.__getitem__):
        widths[c] = max(3, min(natural[c], remaining // left))
        remaining -= widths[c]
        left -= 1
    return widths


def length(spans: list[Span]) -> int:
    return sum(len(text) for text, _ in spans)
