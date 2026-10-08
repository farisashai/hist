"""Merge-readiness scan: a fast, deterministic look at what changed, used by `hist ready` and the stop hook.

It never runs the project's checks itself (too slow for a hook); it names the command CI will run instead.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field

from .util import HOME

CONFIG_PATH = os.path.join(HOME, ".config", "agent-history", "checks.json")

CODE_EXTENSIONS = {".go", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".rs", ".java", ".kt", ".swift",
                   ".rb", ".sh", ".sql", ".proto", ".css", ".scss", ".yaml", ".yml", ".toml", ".tf", ".rego"}
HASH_COMMENT_EXTENSIONS = {".py", ".sh", ".rb", ".yaml", ".yml", ".toml", ".tf", ".rego"}

TEST_FILE = re.compile(r"(\.test\.|\.spec\.|_test\.go$|\.stories\.|(^|/)__tests__/|(^|/)tests?/)")
DEBUG_LINE = re.compile(r"\bconsole\.(log|debug)\(|\bdebugger;|\bbreakpoint\(\)|pdb\.set_trace\(|\bdbg!\(")
TODO_LINE = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")
# Comments that narrate the edit instead of explaining the code ("// Now we fetch…", "# Added helper…").
NARRATION = re.compile(
    r"^(now\b|here\b|this (is|will|function|code|component|hook|helper)\b|we (now|need|use|can|then)\b|"
    r"added?\b|adds\b|updated?\b|updates\b|changed?\b|new\b|note:|step \d|first,|then,|finally,|"
    r"removed?\b|refactor(ed)?\b|moved?\b|fix(ed)?\b|keep(s|ing)? (the|this)\b|helper (to|for)\b)", re.I)
# The request asks for tests itself, so new test files are expected rather than suspicious.
ASKS_FOR_TESTS = re.compile(r"\b(add|write|cover|create|with|include|new|missing|regression)\b[^.\n]{0,40}"
                            r"\b(tests?|specs?|stor(y|ies))\b|\b(tests?|specs?|stories) for\b|\bcoverage\b|\btdd\b", re.I)
TEST_SUFFIX = re.compile(r"([._](test|spec|stories))(?=\.[^.]+$)|_test(?=\.go$)")
PATH_TOKEN = re.compile(r"(?<![\w./-])((?:[\w@.-]+/)*[\w@.-]+\.[A-Za-z]{1,6}|(?:[\w@.-]+/)+[\w@.-]*)(?::\d+)?")


@dataclass
class Finding:
    kind: str      # scope | comment | test-file | debug | todo
    path: str
    line: int | None = None
    text: str = ""


@dataclass
class Report:
    root: str
    base: str | None
    files: list[str]
    findings: list[Finding] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)   # commands CI will run for these files
    named_paths: list[str] = field(default_factory=list)

    def by_kind(self, kind: str) -> list[Finding]:
        return [f for f in self.findings if f.kind == kind]

    def to_dict(self) -> dict:
        return {"root": self.root, "base": self.base, "files": self.files, "named_paths": self.named_paths,
                "checks": self.checks, "findings": [vars(f) for f in self.findings]}


# ---------------------------------------------------------------- git

def git(root: str, *args: str) -> str | None:
    try:
        result = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def repo_root(cwd: str | None) -> str | None:
    if not cwd or not os.path.isdir(cwd):
        return None
    out = git(cwd, "rev-parse", "--show-toplevel")
    return out.strip() if out else None


def merge_base(root: str) -> str | None:
    """Where this branch left the default branch (origin/HEAD, else origin/main, main, master)."""
    candidates = []
    origin_head = git(root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if origin_head:
        candidates.append(origin_head.strip())
    candidates += ["origin/main", "origin/master", "main", "master"]
    for ref in candidates:
        base = git(root, "merge-base", "HEAD", ref)
        if base:
            return base.strip()
    return None


def dirty_files(root: str) -> list[str]:
    """Uncommitted changes and untracked files, relative to the repo root."""
    out = git(root, "status", "--porcelain", "--untracked-files=all") or ""
    files = []
    for line in out.splitlines():
        path = line[3:]
        if " -> " in path:  # rename: keep the new name
            path = path.split(" -> ", 1)[1]
        files.append(path.strip('"'))
    return files


def snapshot(root: str) -> dict:
    """What the branch changes right now: the merge base, plus the content (git blob id) of every file that
    differs from it, committed or not."""
    base = merge_base(root) or (git(root, "rev-parse", "HEAD") or "").strip()
    paths = branch_files(root, base)
    return {"version": 2, "base": base, "files": dict(zip(paths, worktree_blobs(root, paths)))}


def changed_since(root: str, before: dict) -> list[str]:
    """Files whose content this turn changed: edited, created or deleted since the snapshot.

    Committing doesn't change content, so it doesn't count, and neither does a rebase or merge that only
    brings in main's files (clean against the old base and clean against the new one)."""
    if before.get("version") != 2:
        return legacy_changed_since(root, before)
    now = snapshot(root)
    candidates = sorted(set(before["files"]) | set(now["files"]))
    base_before, base_now = tree_blobs(root, before["base"], candidates), tree_blobs(root, now["base"], candidates)
    changed = []
    for path in candidates:
        was = before["files"].get(path, base_before.get(path, "missing"))
        is_now = now["files"].get(path, base_now.get(path, "missing"))
        if was != is_now:
            changed.append(path)
    return changed


def worktree_blobs(root: str, paths: list[str]) -> list[str]:
    present = [path for path in paths if os.path.isfile(os.path.join(root, path))]
    hashes = {}
    if present:
        try:
            out = subprocess.run(["git", "-C", root, "hash-object", "--stdin-paths"], input="\n".join(present),
                                 capture_output=True, text=True, timeout=10).stdout.split()
        except (OSError, subprocess.TimeoutExpired):
            out = []
        hashes = dict(zip(present, out)) if len(out) == len(present) else {path: file_hash(os.path.join(root, path)) for path in present}
    return [hashes.get(path, "missing") for path in paths]


def tree_blobs(root: str, commit: str, paths: list[str]) -> dict:
    """{path: blob id} at `commit`, for those of `paths` that exist there."""
    if not commit or not paths:
        return {}
    out = git(root, "ls-tree", "-r", "--full-tree", commit, "--", *paths) or ""
    blobs = {}
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        blobs[path] = meta.split()[2]
    return blobs


def legacy_changed_since(root: str, before: dict) -> list[str]:
    """Snapshots taken before version 2: HEAD plus a content hash of every dirty file."""
    dirty = {path: file_hash(os.path.join(root, path)) for path in dirty_files(root)}
    changed = {path for path in set(before["files"]) | set(dirty) if before["files"].get(path) != dirty.get(path)}
    head = (git(root, "rev-parse", "HEAD") or "").strip()
    if before.get("head") and head and before["head"] != head:
        changed.update((git(root, "diff", "--name-only", before["head"], head) or "").split())
    return sorted(changed)


def file_hash(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()
    except OSError:
        return "missing"


def branch_files(root: str, base: str | None) -> list[str]:
    """Everything this branch changes: commits since the merge base plus uncommitted work."""
    committed = git(root, "diff", "--name-only", base) if base else ""
    return sorted(set((committed or "").split()) | set(dirty_files(root)))


# ---------------------------------------------------------------- scan

def scan(root: str, files: list[str] | None = None, prompt: str | None = None) -> Report:
    """Check `files` (default: the whole branch) for the things you usually send agents back for."""
    base = merge_base(root)
    files = files if files is not None else branch_files(root, base)
    report = Report(root=root, base=base, files=files)

    report.named_paths = existing_paths(root, named_paths(prompt or ""))
    if report.named_paths:
        for path in files:
            if not any(matches_named(path, named) for named in report.named_paths):
                report.findings.append(Finding("scope", path))

    expect_tests = bool(prompt and ASKS_FOR_TESTS.search(prompt))
    untracked = set((git(root, "ls-files", "--others", "--exclude-standard") or "").split())
    added_files = set((git(root, "diff", "--name-only", "--diff-filter=A", base) or "").split()) if base else set()
    for path in files:
        if not expect_tests and TEST_FILE.search(path) and (path in untracked or path in added_files):
            report.findings.append(Finding("test-file", path))
        previous = None  # (line number, text) of the previous added line, to spot comment continuations
        for line_no, text in added_lines(root, base, path, untracked=path in untracked):
            findings = line_findings(path, line_no, text)
            if any(f.kind == "comment" for f in findings) and continues_comment(root, path, line_no, previous):
                findings = [f for f in findings if f.kind != "comment"]
            report.findings += findings
            previous = (line_no, text)

    report.checks = check_commands(root, files)
    return report


def added_lines(root: str, base: str | None, path: str, untracked: bool):
    """(line number, text) for each line this branch adds to `path`."""
    if os.path.splitext(path)[1] not in CODE_EXTENSIONS:
        return
    full_path = os.path.join(root, path)
    if untracked:
        try:
            with open(full_path, errors="replace") as f:
                for number, line in enumerate(f, 1):
                    yield number, line.rstrip("\n")
        except OSError:
            pass
        return
    diff = git(root, "diff", "-U0", base or "HEAD", "--", path) or ""
    line_no = 0
    for line in diff.splitlines():
        hunk = re.match(r"@@ -\S+ \+(\d+)", line)
        if hunk:
            line_no = int(hunk.group(1))
        elif line.startswith("+") and not line.startswith("+++"):
            yield line_no, line[1:]
            line_no += 1


def line_findings(path: str, line_no: int, text: str) -> list[Finding]:
    findings = []
    comment = comment_body(path, text)
    if comment is not None and NARRATION.match(comment):
        findings.append(Finding("comment", path, line_no, text.strip()[:120]))
    if DEBUG_LINE.search(text):
        findings.append(Finding("debug", path, line_no, text.strip()[:120]))
    if comment is not None and TODO_LINE.search(comment):  # only real TODO comments, not code that mentions TODO
        findings.append(Finding("todo", path, line_no, text.strip()[:120]))
    return findings


def continues_comment(root: str, path: str, line_no: int, previous: tuple[int, str] | None) -> bool:
    """True when the line above is also a comment: a narration word mid-paragraph ("// moved, and the…")
    is prose, not the start of a narrating comment."""
    if previous and previous[0] == line_no - 1:
        above = previous[1]
    else:
        try:
            with open(os.path.join(root, path), errors="replace") as f:
                lines = f.read().splitlines()
        except OSError:
            return False
        above = lines[line_no - 2] if 2 <= line_no <= len(lines) + 1 else ""
    body = comment_body(path, above)
    return bool(body) and not above.strip().endswith(("*/", "-->"))


def comment_body(path: str, line: str) -> str | None:
    """The text of a full-line comment, or None if the line is code."""
    stripped = line.strip()
    extension = os.path.splitext(path)[1]
    for marker in ("//", "/*", "{/*", "<!--", "*"):
        if stripped.startswith(marker) and not stripped.startswith("*/"):
            return stripped[len(marker):].strip()
    if extension in HASH_COMMENT_EXTENSIONS and stripped.startswith("#") and not stripped.startswith("#!"):
        return stripped[1:].strip()
    return None


def named_paths(prompt: str) -> list[str]:
    """File and directory names the request mentions, e.g. 'web/src/a.tsx', 'button-group.tsx'."""
    names = []
    for match in PATH_TOKEN.finditer(prompt):
        token = match.group(1).strip("./")
        if token and "://" not in prompt[max(0, match.start() - 3):match.start()] and not token.startswith("www."):
            names.append(token)
    return list(dict.fromkeys(names))


def existing_paths(root: str, names: list[str]) -> list[str]:
    """Keep only names that are real paths in the repo (or the basename of a tracked file), so prose like
    "and/or" never narrows the scope."""
    if not names:
        return []
    tracked = (git(root, "ls-files") or "").splitlines()
    basenames = {os.path.basename(path) for path in tracked}
    return [name for name in names if os.path.exists(os.path.join(root, name)) or name in basenames]


def matches_named(path: str, named: str) -> bool:
    """`path` is the named file or inside the named directory; a file's own test/story counts as the file."""
    source = TEST_SUFFIX.sub("", path)
    if source != path and matches_named(source, named):
        return True
    if "/" not in named:
        return os.path.basename(path) == named or f"/{named}/" in f"/{path}"
    return path == named or path.startswith(named.rstrip("/") + "/") or path.endswith("/" + named)


# ---------------------------------------------------------------- CI commands

def check_commands(root: str, files: list[str]) -> list[str]:
    """The command(s) CI runs for these files: from ~/.config/agent-history/checks.json if it matches,
    else the nearest package.json with a check:all script."""
    commands = []
    for rule in load_check_config():
        if os.path.basename(root) == rule.get("repo", os.path.basename(root)) and \
                any(fnmatch.fnmatch(path, rule.get("paths", "*")) for path in files):
            commands.append(rule["command"])
    if commands:
        return list(dict.fromkeys(commands))

    for package_dir in sorted({nearest_package_dir(root, path) for path in files} - {None}):
        scripts = read_scripts(os.path.join(root, package_dir, "package.json"))
        script = next((name for name in ("check:all", "check") if name in scripts), None)
        if script:
            prefix = f"cd {package_dir} && " if package_dir != "." else ""
            commands.append(f"{prefix}CI=true pnpm {script}")
    return commands


def load_check_config() -> list[dict]:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f).get("checks", [])
    except (OSError, ValueError):
        return []


def nearest_package_dir(root: str, path: str) -> str | None:
    directory = os.path.dirname(path)
    while True:
        if os.path.exists(os.path.join(root, directory, "package.json")):
            return directory or "."
        if not directory:
            return None
        directory = os.path.dirname(directory)


def read_scripts(package_json: str) -> dict:
    try:
        with open(package_json) as f:
            return json.load(f).get("scripts", {}) or {}
    except (OSError, ValueError):
        return {}
