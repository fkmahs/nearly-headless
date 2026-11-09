"""Files that never go to GitHub: spreadsheets, client documents, archives and Office lock files (PATTERNS), and any
other binary file outside docs/, where design images are allowed. One rule for every repo, owned by nearly-headless.

Stop at the source: `nh setup` (and so `nh update`) writes PATTERNS into each repo's local git exclude file, in the
Git folder its worktrees share, inside a marked block. Git then never offers those files to add, and nothing is
committed to the repo. The owner's own lines in that file are never touched.

Catch the rest: `nh check` reports every file on the base branch (as GitHub has it) that matches PATTERNS, or that
git itself counts as binary outside docs/, with the command that takes it out of git and keeps the local copy. It
never deletes anything. An image a deliberate visual test uses may stay, one file at a time, on the owner's yes
(`nh check <repo> --ack file:<path>`).

One exception is the kit's own, in the check only: invoices the invoicing toolkit made before 1.30 keep their frozen
statement as billing/<ID>.csv (BILLING), already in git and never rewritten, so the check doesn't flag them. Since 1.30
the statement is the readable billing/<ID>.md alone, so the exclude block keeps every new .csv out, billing/ included.

Binary means what git means: `git diff --numstat` prints "-" for a file whose first 8000 bytes hold a NUL byte (or
that .gitattributes marks binary). It is the same test GitHub uses to show "Binary file not shown", covers every
binary type (images, executables, databases, parquet) without a list of extensions to keep up, and is one git call
per repo.
"""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

from . import core
from .core import KitError

PATTERNS = ("*.xlsx", "*.xls", "*.xlsm", "*.xlsb", "*.csv", "*.pdf", "*.docx", "*.doc", "*.pptx", "*.zip", "~$*")
BILLING = "billing/*.csv"  # statements of invoices made before 1.30 (toolkits/invoicing), already in git
IMAGES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp")  # a flagged image may be allowed one by one
DOCS = "docs/"  # binaries here (design images) belong in GitHub
START = "# nearly-headless: files that never go to GitHub. nh setup keeps this block; add your own lines outside it."
END = "# end of the kit's block"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git's empty tree, to diff every file against


def exclude_file(top: Path) -> Path:
    """The repo's local exclude file, in the Git folder its worktrees share, so one block covers them all."""
    return core.git_common_dir(top) / "info" / "exclude"


def _block() -> list[str]:
    return [START, *PATTERNS, END]


def _with_block(text: str) -> str:
    """The exclude file with the kit's block current: replaced where it is, else added at the
    end. A block is a start line and the first end line before the next start; a start with no end is left as it
    is, so a later run never swallows the owner's lines. Every other line stays as it was."""
    lines = text.splitlines()
    starts = [i for i, x in enumerate(lines) if x.startswith("# nearly-headless:")]
    for n, start in enumerate(starts):
        stop = starts[n + 1] if n + 1 < len(starts) else len(lines)
        end = next((i for i in range(start + 1, stop) if lines[i] == END), None)
        if end is not None:
            lines[start:end + 1] = _block()
            break
    else:
        lines += ([""] if lines and lines[-1].strip() else []) + _block()
    return "\n".join(lines) + "\n"


def _read(path: Path) -> str:
    """The exclude file's text; KitError when it isn't UTF-8, so it is never rewritten with characters lost."""
    try:
        return path.read_bytes().decode("utf-8") if path.is_file() else ""
    except UnicodeDecodeError as exc:
        raise KitError(f"{core.posix(path)} is not UTF-8 text; left as it is. Add the kit's block by hand "
                           "(wk/keepout.py) or save the file as UTF-8, then run nh setup") from exc


def installed(top: Path) -> bool:
    try:
        text = _read(exclude_file(top))
    except KitError:
        return False
    return "\n".join(_block()) in "\n".join(text.splitlines())


def install(top: Path) -> dict:
    """Write or refresh the kit's block in the repo's exclude file; nothing changes when it is already current, and a
    file that can't be read as UTF-8 is never rewritten."""
    path = exclude_file(top)
    try:
        text = _read(path)
    except KitError as exc:
        return {"installed": False, "file": core.posix(path), "why": str(exc)}
    new = _with_block(text)
    if new.splitlines() == text.splitlines():
        return {"installed": True, "changed": False, "file": core.posix(path)}
    core.atomic_text(path, new)
    return {"installed": True, "changed": True, "file": core.posix(path)}


def _billing(path: str) -> bool:
    folder, _, name = path.rpartition("/")
    return folder == BILLING.split("/")[0] and fnmatch(name.lower(), BILLING.split("/")[1])


def tracked(top: Path, ref: str) -> list[tuple[str, str]]:
    """Files at ref that don't belong in GitHub, with why: matching PATTERNS (any folder; older invoice statements aside),
    or binary outside docs/. KitError when git can't list them (no such branch, or git stuck)."""
    out = core.git(top, "-c", "core.quotepath=false", "diff", "--numstat", "-z", "--no-renames", "--no-textconv",
                   "--no-ext-diff", EMPTY_TREE, ref)
    found = []
    for record in out.split("\0"):
        added, _, rest = record.partition("\t")
        path = rest.partition("\t")[2]
        if not path or _billing(path):
            continue
        name = path.rsplit("/", 1)[-1].lower()
        match = next((p for p in PATTERNS if fnmatch(name, p)), None)
        if match:
            found.append((path, f"a spreadsheet or client file ({match})"))
        elif added == "-" and not path.startswith(DOCS):
            found.append((path, "a binary file outside docs/"))
    return found
