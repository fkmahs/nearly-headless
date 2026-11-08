"""Production compare for the SDF kit: is what a release sends already what production runs?

Pure functions, no NetSuite or git calls: sdf.py imports production's copies and reads git, then asks here whether
two copies are the same. Files are the same when their bytes match after line endings are normalised. Objects (SDF
XML) are the same by meaning: what NetSuite adds or reorders on its own is ignored (see object_differences)."""

import difflib
import xml.etree.ElementTree as ET

# Fields NetSuite changes on its own whatever the XML says, so a difference in them is never someone's change:
# - applyformatting: NetSuite sets T on currency fields deployed with F (a sandbox deployed from the same XML does it);
# - onparentdelete: an import exports it blank on script parameters the XML sets to NO_ACTION.
NETSUITE_OWN = ("applyformatting", "onparentdelete")
DEFAULTS = ("", "F")   # what NetSuite writes for a field one side leaves out


def same_file(a: bytes, b: bytes) -> bool:
    return a.replace(b"\r\n", b"\n") == b.replace(b"\r\n", b"\n")


def _canon(el) -> str:
    kids = list(el)
    if not kids:
        return (el.text or "").strip()
    return ",".join(sorted(f"{k.tag}={_canon(k)}" for k in kids if _canon(k) != ""))


def flatten(text: str) -> dict:
    """{path: value} for every leaf of an SDF XML. A child with a scriptid is keyed by it (tag[scriptid]); repeated
    children without one by their content, so order never matters. {} when the XML can't be read."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return {}
    out = {}

    def walk(el, path):
        kids = list(el)
        if not kids:
            out[path] = (el.text or "").strip()
            return
        tags = [k.tag for k in kids]
        for k in kids:
            if k.get("scriptid"):
                key = f"{k.tag}[{k.get('scriptid')}]"
            elif tags.count(k.tag) > 1:
                key = f"{k.tag}{{{_canon(k)}}}"
            else:
                key = k.tag
            walk(k, f"{path}/{key}")

    walk(root, root.tag)
    return out


def _norm(v: str) -> str:
    return "|".join(sorted(v.split("|"))) if "|" in v else v   # audiences and role lists come back in any order


def _ignored(key: str, ignore) -> bool:
    leaf = key.rsplit("/", 1)[-1]
    for rule in ignore:
        sid, _, tag = rule.rpartition("/")
        if leaf == tag and (not sid or f"[{sid}]" in key):
            return True
    return False


def object_differences(git_text: str, prod_text: str, ignore=()) -> list[str]:
    """The real differences between git's copy of an object and production's, as 'path: git=... production=...'
    lines ([] when they are the same by meaning). Ignored, because NetSuite does them on its own or a deploy of
    git's copy would not change them:
    - order of children, and of '|' lists (audiences, roles);
    - a field git leaves blank, whatever production holds;
    - a field only one side has, at '' or 'F' (NetSuite's defaults);
    - a field only production has, directly on an element git's copy also has (NetSuite fills it in: enabledle, a
      one-time deployment's start date; deploys merge, so sending git's copy leaves it as it is). A whole element
      only production has (a deployment or field made in the UI) is a real difference, and so is a row keyed by
      its content (a role permission, a list value, a translation) that only production has;
    - NETSUITE_OWN fields, and each rule in ignore ('tag' or '<scriptid>/tag', from the repo's sdf.toml)."""
    a, b = flatten(git_text), flatten(prod_text)
    if not a or not b:
        return [] if git_text.strip() == prod_text.strip() else ["the XML could not be read; compared as text: differs"]
    rules = tuple(NETSUITE_OWN) + tuple(ignore)
    elements = {"/".join(k.split("/")[:i]) for k in a for i in range(1, k.count("/") + 1)}   # what git's copy has
    real = []
    for k in sorted(set(a) | set(b)):
        va, vb = a.get(k), b.get(k)
        if _ignored(k, rules):
            continue
        if va is not None and vb is not None:
            if _norm(va) == _norm(vb) or va == "":
                continue
            real.append(f"{k}: git={va!r} production={vb!r}")
        elif va is None:
            # A field directly on an element git has: NetSuite filled it in. A row keyed by its content (a role's
            # permission, a list's value, a translation) is keyed by every non-blank field, so git having the row's
            # key means production only added blanks to it (an export writes <restriction></restriction>); a row
            # only production has was added in production and is always real, whatever its values.
            if k.rsplit("/", 1)[0] in elements or ("{" not in k and vb in DEFAULTS):
                continue
            real.append(f"{k}: only in production = {vb!r}")
        else:
            if va in DEFAULTS:
                continue
            real.append(f"{k}: only in git = {va!r}")
    return real


def same_object(git_text: str, prod_text: str, ignore=()) -> bool:
    return not object_differences(git_text, prod_text, ignore)


def text_diff(path: str, prod: bytes, git: bytes, rev: str) -> str:
    """A unified diff from production's copy to git's (line endings normalised), to read before deciding."""
    p = prod.replace(b"\r\n", b"\n").decode("utf-8", "replace").splitlines(keepends=True)
    g = git.replace(b"\r\n", b"\n").decode("utf-8", "replace").splitlines(keepends=True)
    return "".join(difflib.unified_diff(p, g, f"production/{path}", f"git {rev}/{path}"))
