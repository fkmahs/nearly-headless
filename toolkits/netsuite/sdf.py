"""SuiteCloud (SDF) kit: deploys and imports for a NetSuite account customization project, for every client repo.

What is client-specific (auth IDs, login domains, .env key prefixes, the File Cabinet folder uploads may touch, where
reference imports go) comes from the repo's sdf.toml; see the toolkit's README. Run it from the repo (or through the
repo's own thin entry point):

    py <nearly-headless>/toolkits/netsuite/sdf.py <command> sb|prod ...

Every command names its environment explicitly (sb or prod); the kit points the SuiteCloud CLI at the matching auth
ID by rewriting the git-ignored project.json before each call. Turns and gates come from the kit's `wk`. Guard rails:

- account check: before every call, the auth ID's account must be the right kind (sandbox or not) and match the
  account ID in the repo's .env.
- deploy and upload need a committed src/. What a deploy sends is picked by PR:
  - sandbox: the src/ files and objects this branch changed since its merge-base with origin/main (plus the script
    file of each changed script record);
  - production (--pr N): the src/ files and objects the PR's merge commit changed.
- production runs only after the merge: on main, clean and level with origin/main (`nh gate release`), for a merged
  PR (--pr N) whose merge commit is in HEAD, with the owner's approval comment on that PR for its head commit
  (`nh gate approved --pr N --action "deploy to production"`, or "upload to production"); a release is tagged
  prod-<date>-pr<N>, listing the files it sent.
- sandbox turns: a sandbox deploy or upload is refused unless this branch holds a turn on every file it sends
  (`nh turn check sandbox <files>`). `release sb` puts main's version of those files back, then frees the turn.
- production compare: before a production deploy or upload (and in its dry run), production's current copy of every
  file and object it sends is imported read-only into a temporary folder and compared with what will be sent:
  already live (no approval needed), changed since production's version (needs the PRs that changed it since),
  new (needs the PRs that added it), or a version git never had (refused: bring production's change into git first).
- upload is the code-only path: it pushes named files under the repo's own folder and nothing else, and refuses
  object changes. With the repo's replace-in-place flag it overwrites other /SuiteScripts files in place (local copy
  with '|' stored as '-'), after backing up the account's copy under <reference>/backups/<date>-<env>-<commit>/.
- imports with --reference are moved out of src/ into <reference>/<YYYY-MM-DD>/ so they can never be deployed.
- release ancestry (production): refuses unless every prod-* tag is in HEAD's history.
- tool version: deploy and upload refuse when origin/main has changes to the repo's tool files (sdf.toml, its
  entry point) this branch lacks; every command refuses when this nearly-headless is older than sdf.toml's `requires`.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

KIT = Path(__file__).resolve().parent
sys.path.insert(0, str(KIT))
import query  # noqa: E402  (the toolkit's .env reader)
import sdf_compare  # noqa: E402

KIT_HOME = KIT.parent.parent            # this nearly-headless checkout
ROOT = Path.cwd()                       # the client repo; configure() sets it
SRC = ROOT / "src"
CABINET = SRC / "FileCabinet"
PROJECT_JSON = "project.json"
CONFIG_FILE = "sdf.toml"
DEFAULTS = {
    "requires": "",                     # required: the oldest nearly-headless the repo needs
    "own_folder": "",                   # e.g. /SuiteScripts/ACME/: the only folder a plain upload touches
    "wrapper_env": "WK_SDF_WRAPPER",    # set for every CLI call; the repo's suitecloud.config.js can require it
    "reference": "reference",           # --reference imports and replace-in-place backups go under it
    "replace_in_place_flag": "",        # e.g. replace-vendor: enables `upload --<flag>`
    "tool_files": ["sdf.toml"],         # the repo's own tool files: a branch must have main's version
    "entry": "",                        # how the repo runs this kit, for hints (default: py <kit>/sdf.py)
    "sb": {}, "prod": {},
    "compare": {},
}
CONFIG = dict(DEFAULTS)
NH_KIT = KIT_HOME   # where wk is found when the wk on PATH is not the kit's


def fail(msg):
    print(f"sdf: {msg}", file=sys.stderr)
    sys.exit(2)


def configure(root):
    """Point the kit at a client repo: its folder and its sdf.toml."""
    global ROOT, SRC, CABINET, CONFIG
    ROOT = Path(root).resolve()
    SRC, CABINET = ROOT / "src", ROOT / "src" / "FileCabinet"
    path = ROOT / CONFIG_FILE
    if not path.is_file():
        fail(f"no {CONFIG_FILE} in {ROOT}; a repo that uses this kit describes itself there (see the toolkit's README)")
    try:
        CONFIG = {**DEFAULTS, **tomllib.loads(path.read_text(encoding="utf-8"))}
    except tomllib.TOMLDecodeError as e:
        fail(f"{CONFIG_FILE} can't be read: {e}")
    missing = [] if str(CONFIG.get("requires") or "").strip() else ["requires"]
    for env in ("sb", "prod"):
        missing += [f"[{env}] {key}" for key in ("auth_id", "env_prefix") if not str(CONFIG[env].get(key) or "").strip()]
    if missing:
        fail(f"{CONFIG_FILE} in {ROOT} is missing {', '.join(missing)}; each guard depends on them (see the toolkit's "
             "README for every key)")
    return CONFIG


def find_root(start=None):
    """The nearest folder from start up that holds sdf.toml."""
    start = Path(start or Path.cwd()).resolve()
    for folder in (start, *start.parents):
        if (folder / CONFIG_FILE).is_file():
            return folder
    fail(f"no {CONFIG_FILE} found from {start} up; run this from a repo that uses the SDF kit")


def env_cfg(env):
    return CONFIG[env]


def auth_ids():
    return {env: CONFIG[env]["auth_id"] for env in ("sb", "prod")}


def own_folder():
    folder = CONFIG.get("own_folder") or ""
    return folder if folder.endswith("/") else folder + "/"


def replace_flag():
    return CONFIG.get("replace_in_place_flag") or ""


def entry():
    """The command people run for this kit in this repo, for hints."""
    return CONFIG.get("entry") or f"py {(KIT / 'sdf.py').as_posix()}"


def wrapper_env(extra=None):
    """The environment for every SuiteCloud CLI call: the repo's suitecloud.config.js may refuse writes without it."""
    return dict(os.environ, **{CONFIG.get("wrapper_env") or "WK_SDF_WRAPPER": "1"}, **(extra or {}))


def suitecloud(args, auth_id=None, capture=False, env=None, cwd=None):
    """cwd: the project folder (default the repo; release sb and the production compare use a checkout of a commit)."""
    exe = shutil.which("suitecloud")
    if not exe:
        fail("SuiteCloud CLI not found on PATH (npm install -g @oracle/suitecloud-cli)")
    cwd = cwd or ROOT
    if auth_id:
        (Path(cwd) / PROJECT_JSON).write_text(json.dumps({"defaultAuthId": auth_id}, indent=1) + "\n")
    cmd = [exe, *args]
    print("> suitecloud " + " ".join(args) + (f"   [auth: {auth_id}]" if auth_id else ""), flush=True)
    env = env or wrapper_env()
    if capture:
        res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
        return res.returncode, res.stdout + res.stderr
    return subprocess.run(cmd, cwd=cwd, env=env).returncode, None


# --- The '|' path agent: File Cabinet folders may hold '|', which Windows paths cannot ---

PATHFIX_SRC = KIT / "sdf_pathfix" / "SdfPathAgent.java"
# JDK-internal class-file API used by the agent (JDK 21); the SDF CLI already requires Java 21.
PATHFIX_EXPORTS = ("jdk.internal.classfile", "jdk.internal.classfile.instruction", "jdk.internal.classfile.constantpool")


def pathfix_build_dir():
    """A per-user cache folder, keyed by the agent's source, so the kit's checkout never holds build output."""
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache") / "nearly-headless" / "sdf-pathfix"
    return base / hashlib.sha256(PATHFIX_SRC.read_bytes()).hexdigest()[:12]


def pathfix_agent():
    """Build (once) the Java agent that maps '|' to '-' in the local paths SDF reads and writes.

    File Cabinet folders such as '/SuiteScripts/X | Y' cannot exist on Windows, so a plain file:import fails. The
    agent only renames what SDF writes (import) or reads (upload) locally; nothing sent to NetSuite changes."""
    build = pathfix_build_dir()
    jar = build / "sdf-path-agent.jar"
    if jar.is_file():
        return jar
    javac = shutil.which("javac") or fail("javac not found on PATH (a JDK 21 is needed for imports)")
    classes = build / "classes"
    shutil.rmtree(build, ignore_errors=True)
    classes.mkdir(parents=True)
    exports = [a for p in PATHFIX_EXPORTS for a in ("--add-exports", f"java.base/{p}=ALL-UNNAMED")]
    res = subprocess.run([javac, *exports, "-d", str(classes), str(PATHFIX_SRC)], capture_output=True, text=True)
    if res.returncode != 0:
        fail("could not build the import path agent:\n" + res.stdout + res.stderr)
    import zipfile
    manifest = "Manifest-Version: 1.0\nPremain-Class: SdfPathAgent\nAdd-Exports: " + " ".join(f"java.base/{p}" for p in PATHFIX_EXPORTS) + "\n"
    with zipfile.ZipFile(jar, "w") as z:
        z.writestr("META-INF/MANIFEST.MF", manifest)
        for f in classes.rglob("*.class"):
            z.write(f, f.relative_to(classes).as_posix())
    return jar


def import_env():
    return wrapper_env({"JAVA_TOOL_OPTIONS": f'-javaagent:"{pathfix_agent()}"'})


# --- Accounts ---

def auth_info(auth_id):
    """Return {'account': ..., 'role': ..., 'url': ...} or None if the auth ID is missing."""
    code, out = suitecloud(["account:manageauth", "--info", auth_id], capture=True)
    if code != 0:
        return None
    info = {}
    for key, pat in (("account", r"Account(?: ID)?:\s*(\S+)"), ("role", r"Role(?: ID)?:\s*(.+)"), ("url", r"(?:URL|Domain):\s*(\S+)")):
        m = re.search(pat, out, re.IGNORECASE)
        if m:
            info[key] = m.group(1).strip()
    return info or {"raw": out.strip()}


def env_file():
    """The repo's .env: the nearest one from the repo up (query.find_env_file), else the main checkout's, so one
    .env serves every worktree of the repo, wherever the worktree lives."""
    found = query.find_env_file(ROOT)
    if found.is_file() or os.environ.get("NETSUITE_ENV_FILE"):
        return found
    res = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=ROOT,
                         capture_output=True, text=True)
    main = Path(res.stdout.strip()).parent / ".env" if res.returncode == 0 and res.stdout.strip() else None
    return main if main and main.is_file() else found


def expected_account(env):
    """The account ID the repo's .env names for env ('' when it names none)."""
    prefix = env_cfg(env)["env_prefix"]
    return query.load_env_file(env_file()).get(prefix + "ACCOUNT_ID", "")


def check_env(env):
    """Confirm the auth ID exists and points at the right kind of account, the one .env names."""
    auth_id = auth_ids()[env]
    info = auth_info(auth_id)
    if info is None:
        fail(f"auth ID {auth_id} is not set up; see the SDF playbook (login {env})")
    account = info.get("account", "")
    if not account:
        fail(f"could not read the account ID for {auth_id}; refusing")
    is_sandbox = "_SB" in account.upper()
    if env == "sb" and not is_sandbox:
        fail(f"{auth_id} points at account {account}, which is not a sandbox; refusing")
    if env == "prod" and is_sandbox:
        fail(f"{auth_id} points at sandbox account {account}; expected production")
    expected = expected_account(env)
    if not expected:
        prefix = env_cfg(env)["env_prefix"]
        fail(f"this repo's .env names no {prefix}ACCOUNT_ID (sdf.toml [{env}] env_prefix = \"{prefix}\"), so the "
             f"account behind {auth_id} can't be checked; refusing. Fix the prefix in sdf.toml or add the key to .env")
    if expected.upper().replace("-", "_") != account.upper().replace("-", "_"):
        fail(f"{auth_id} points at account {account}, but .env expects {expected}; refusing")
    return auth_id


# --- nearly-headless: nh gate for production, nh turn for the sandbox ---

SANDBOX = "sandbox"  # the nh turn resource; its items are the src/ paths a sandbox deploy or upload sends


def is_nh_kit(home):
    """True for a nearly-headless checkout: its wk package, not another tool's 'wk' (an old command center's)."""
    return (home / "wk" / "__main__.py").is_file() and (home / "wk" / "turns.py").is_file()


def nh_kit_home():
    """The nearly-headless whose wk runs turns and gates: the one whose bin/ holds the wk on PATH (the installed one), else
    the checkout this kit is in. Refuses otherwise."""
    found = shutil.which("nh") or shutil.which("wk")
    homes = ([Path(found).resolve().parent.parent] if found else []) + [NH_KIT]
    for home in homes:
        if is_nh_kit(home):
            return home
    fail(f"nearly-headless was not found (looked at {', '.join(h.as_posix() for h in homes)}); sandbox turns and production "
         "gates need it. Put the kit's bin/ folder on PATH")


def wk_command():
    """(argv prefix, env) to run the kit's wk: always `python -m wk` from the nearly-headless checkout (what its bin/wk.cmd
    does), so paths with '|' and '&' never pass through cmd.exe and no other tool's 'wk' is run."""
    home = nh_kit_home()
    path = os.pathsep.join(filter(None, [str(home), os.environ.get("PYTHONPATH")]))
    return [sys.executable, "-m", "wk"], dict(os.environ, PYTHONPATH=path)


def wk(*args):
    """Run `wk <args>` for this repo; returns (exit code, output). wk exits 1 when it refuses."""
    cmd, env = wk_command()
    res = subprocess.run([*cmd, "--cwd", str(ROOT), *args], cwd=ROOT, capture_output=True, env=env,
                         text=True, encoding="utf-8", errors="replace")
    return res.returncode, (res.stdout + res.stderr).strip()


def wk_refusal(*args):
    """The refusal from `wk <args>` (None when it passes)."""
    code, out = wk(*args)
    return f"nh {' '.join(args)}: {out}" if code else None


def gh_json(*args):
    res = subprocess.run(["gh", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode != 0:
        fail(f"gh {' '.join(args)} failed: {res.stderr.strip()}")
    return json.loads(res.stdout or "{}")


def is_ancestor(a, b):
    return subprocess.run(["git", "merge-base", "--is-ancestor", a, b], cwd=ROOT, capture_output=True).returncode == 0


def has_commit(rev):
    return bool(rev) and subprocess.run(["git", "cat-file", "-e", rev + "^{commit}"], cwd=ROOT,
                                        capture_output=True).returncode == 0


def prod_action(action):
    """The approval's action text for nh gate approved: 'deploy to production' / 'upload to production'."""
    return f"{action} to production"


def pr_label(pr):
    return f"PR #{pr}"


def merge_parents(rev):
    return git("rev-list", "--parents", "-n", "1", rev).split()[1:]


def not_a_merge_commit(pr, merge):
    """The refusal when PR #pr's merge commit is not a real two-parent merge (a squash or rebase merge): then
    <merge>^1..<merge> would not be the PR's whole change, and its commits would not be in main's history."""
    if len(merge_parents(merge)) == 2:
        return None
    return (f"PR #{pr} was not merged with a merge commit ({merge[:7]} has one parent: a squash or rebase merge). "
            "Production releases need a merge commit, so what the PR changed is exactly what its merge commit brought. "
            "Merge PRs with 'Create a merge commit' only; this one has to be released through a new PR merged that way")


def merged_pr(pr, head="HEAD"):
    """(refusal or None, PR data): a production release ships a merged PR (merge, then release from main):
    the PR's merge commit must be a two-parent merge commit, and in head."""
    data = gh_json("pr", "view", str(pr), "--json", "state,mergeCommit,headRefOid,url,comments")
    if data.get("state") != "MERGED":
        return (f"PR #{pr} is {(data.get('state') or 'unknown').lower()}, not merged. Production receives deploys only "
                f"after the merge, from main: merge it, pull main, then release"), data
    merge = (data.get("mergeCommit") or {}).get("oid") or ""
    if not has_commit(merge) or not is_ancestor(merge, head):
        return f"PR #{pr}'s merge commit {merge[:7] or '(none)'} is not in this checkout's main; pull main first", data
    return not_a_merge_commit(pr, merge), data


def approval_comment(data, accepted):
    """The body of the PR comment nh gate approved accepted (accepted: its JSON output, which names the comment's
    URL; that comment has the configured approver's line for the PR's head). '' when it can't be matched, so then
    only the released PR is covered."""
    try:
        url = (json.loads(accepted or "{}") or {}).get("comment") or ""
    except (ValueError, AttributeError):
        url = ""
    bodies = [c.get("body", "") for c in data.get("comments", []) if url and c.get("url") == url]
    return bodies[-1] if bodies else ""


KIT_FILES = ("toolkits/netsuite", "wk")   # what a production release runs from nearly-headless: this kit and wk's gates


def checkout_refusal(home, what):
    """The refusal when the nearly-headless checkout at home has uncommitted or untracked changes in KIT_FILES, or a HEAD
    not on the kit's origin/main (main ahead only by its own records is fine). None when it is clean and released."""
    def kgit(*args):
        return subprocess.run(["git", *args], cwd=home, capture_output=True, text=True)
    where = Path(home).as_posix()
    dirty = kgit("status", "--porcelain", "--", *KIT_FILES)
    if dirty.returncode != 0:
        return f"{what} at {where} is not a git checkout of nearly-headless, so it can't be checked; production " \
               "releases run only a released nearly-headless (nh update)"
    if dirty.stdout.strip():
        return (f"{what} at {where} has uncommitted changes:\n" + dirty.stdout.rstrip() +
                f"\nProduction releases run only a released nearly-headless: undo those changes in {where} (or run from a "
                "clean nearly-headless with nh update), then run this again")
    kgit("fetch", "-q", "origin", "main")
    on_main = kgit("merge-base", "--is-ancestor", "HEAD", "origin/main").returncode == 0
    # A main ahead only by its own records (wk's .work/ commits not pushed yet) runs main's code all the same.
    if not on_main and kgit("diff", "--quiet", "origin/main", "HEAD", "--", *KIT_FILES).returncode != 0:
        branch = kgit("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        return (f"{what} at {where} is on '{branch}', which is not in the kit's origin/main; production "
                f"releases run only a released nearly-headless. In {where}: git switch main, then nh update (or unset "
                "NH_KIT_HOME if it points at a feature checkout), then run this again")
    return None


def kit_checkout_refusal():
    """A production release runs only released code: the nearly-headless whose wk runs the gates, and this kit's own
    checkout when that is a different one. Their refusals, joined; None when both are clean and on origin/main."""
    homes = {Path(nh_kit_home()).resolve(): "nearly-headless (its wk runs the production gates)"}
    homes.setdefault(Path(KIT_HOME).resolve(), "the SDF kit")
    found = [r for home, what in homes.items() for r in [checkout_refusal(home, what)] if r]
    return "\n".join(found) or None


def prod_gates(action, pr):
    """(refusals, approval, PR data): the production gates' refusals, in order, the approval for the production
    compare, and what gh says about the PR (its merge commit sets what a deploy sends). Each gate's refusal is
    listed, so a dry run can report them all."""
    refusals = [r for r in [kit_checkout_refusal()] if r]
    gate = wk_refusal("gate", "release")
    if gate:
        refusals.append(gate + "\nProduction releases run after the merge, from main: "
                        "git switch main, git pull, then run this again.")
    if not pr:
        return refusals + [f"--pr is needed: the merged PR whose approval covers this production {action}"], None, None
    refusal, data = merged_pr(pr)
    if refusal:
        return refusals + [refusal], None, data
    args = ("gate", "approved", "--pr", str(pr), "--action", prod_action(action))
    code, out = wk(*args)
    if code:
        return refusals + [f"nh {' '.join(args)}: {out}"], None, data
    return refusals, {"label": pr_label(pr), "text": approval_comment(data, out), "pr": pr}, data


def check_prod_gates(action, pr):
    refusals, approval, data = prod_gates(action, pr)
    if refusals:
        fail("\n".join(refusals))
    print(f"sdf: on main, level with origin/main; PR #{pr} merged and approved for {prod_action(action)}")
    return approval, data


def quoted(paths):
    return " ".join(f'"{p}"' if re.search(r"[\s&|]", p) else p for p in paths)


def check_turn(paths):
    """A sandbox deploy or upload sends only files this branch holds a turn on."""
    code, out = wk("turn", "check", SANDBOX, *paths)
    if code:
        fail(f"no sandbox turn on every file this sends ({out.removeprefix('nh: ').removeprefix('wk: ')}).\n"
             f"The supervisor decides whose turn it is; then take it (it needs main merged in first):\n"
             f"  nh turn take {SANDBOX} {quoted(paths)}\n"
             f"When done: {entry()} release sb")


def held_items():
    """The sandbox items this branch's turns hold (live or expired; another branch's take replaces them)."""
    code, out = wk("--json", "turn", "list", SANDBOX)
    if code:
        fail(f"nh turn list failed: {out}")
    branch = git("rev-parse", "--abbrev-ref", "HEAD").strip()
    return [i for g in json.loads(out or "[]") if g.get("holder") == branch for i in g.get("items", [])]


def tag_message(owner, rev, action, files=()):
    """owner: 'PR #N'."""
    return (f"{owner} production {action} of {rev}"
            + ("\n\n" + "\n".join(f"file: {f}" for f in files) if files else ""))


def tag_release(pr, rev, action, files=()):
    """Tag and push prod-<date>-pr<N>, suffixed -2, -3 ... when that tag already exists.
    The tag lists the files it sent ('file: src/...')."""
    base = f"prod-{datetime.date.today().isoformat()}-pr{pr}"
    existing = set(git("tag", "-l", f"{base}*").split())
    tag, n = base, 2
    while tag in existing:
        tag, n = f"{base}-{n}", n + 1
    git("tag", "-a", tag, "-m", tag_message(pr_label(pr), rev, action, files))
    git("push", "origin", tag)
    print(f"sdf: tagged and pushed {tag}")


def tag_owner(name, body):
    """'PR #N' for a tag made by PR (prod-<date>-pr<N>); for an older tag, what it was named after
    (prod-<date>-<ticket>[-n])."""
    m = re.search(r"-pr(\d+)(?:-\d+)?$", name)
    if m:
        return pr_label(m.group(1))
    m = re.match(r"prod-\d{4}-\d{2}-\d{2}-(.+?)(?:-\d+)?$", name)
    return m.group(1) if m else ""


# --- Who changed a file: PRs by their merge commits on main ---

PR_SUBJECT = re.compile(r"(?:pull request|\bPR) #(\d+)", re.I)  # 'Merge pull request #7 from ...', '...: merge PR #97 (...)'


def pr_sort(keys):
    """PR labels by number, then anything else (short SHAs)."""
    return sorted(keys, key=lambda k: (0, int(k.split("#")[1]), "") if k.startswith("PR #") else (1, 0, k))


def first_parent_history(rng):
    """[(full SHA, short SHA, parents, subject, files)] along the first-parent history of rng, newest first; files are
    what each commit changed against its first parent (for a merge: what it brought). No pathspec, because git's
    history simplification would hide a merge that matches its second parent for that path."""
    out = git("log", "--first-parent", "-m", "--name-only", "--format=%x01%H%x00%h%x00%P%x00%s", *rng)
    rows = []
    for block in out.split("\x01")[1:]:
        header, _, names = block.partition("\n")
        full, short, parents, subject = header.split("\x00", 3)
        rows.append((full, short, parents.split(), subject, {n.strip() for n in names.splitlines() if n.strip()}))
    return rows


def change_owners(rng, path, own=None, depth=0, cache=None):
    """([PR labels], [short SHAs]): who changed path along the first-parent history of rng ([b] or ['a..b']).
    A PR's merge commit stands for every commit it brought. Another merge (main merged into a branch) is followed
    into what it brought. Any other commit is its own short SHA, unless own ({full SHA: label}) names it: the
    commits of a PR previewed before its merge. cache: {range: history}, shared by one compare."""
    cache = {} if cache is None else cache
    if tuple(rng) not in cache:
        cache[tuple(rng)] = first_parent_history(rng)
    prs, loose = set(), []
    for full, short, parents, subject, files in cache[tuple(rng)]:
        if path not in files:
            continue
        if own and full in own:
            prs.add(own[full])
            continue
        if len(parents) > 1:
            m = PR_SUBJECT.search(subject)
            if m:
                prs.add(pr_label(m.group(1)))
                continue
            if depth < 8:
                more, extra = change_owners([f"{parents[0]}..{parents[1]}"], path, own, depth + 1, cache)
                prs.update(more)
                loose += [x for x in extra if x not in loose]
                continue
        if short not in loose:
            loose.append(short)
    return pr_sort(prs), loose


def deploy_patterns(head="HEAD"):
    """Regexes for the src/ paths a project deploy sends: the <path> entries of src/deploy.xml
    at head ('~/' is src/, '*' matches files and subfolders), plus src/manifest.xml and
    src/deploy.xml themselves. None (everything counts) when deploy.xml is missing or unreadable."""
    import xml.etree.ElementTree as ET
    try:
        entries = [(e.text or "").strip() for e in ET.fromstring(show_file(head, "src/deploy.xml") or "").iter("path")]
    except ET.ParseError:
        return None
    entries = [e for e in entries if e.startswith("~/")]
    if not entries:
        return None
    regexes = [re.compile("src/" + ".*".join(re.escape(part) for part in e[2:].split("*")) + r"\Z") for e in entries]
    return regexes + [re.compile(r"src/(manifest|deploy)\.xml\Z")]


def is_deployed(path, patterns):
    return patterns is None or any(r.match(path) for r in patterns)


def show_file(rev, path):
    res = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    return res.stdout if res.returncode == 0 else None


def show_bytes(rev_or_blob, path=None):
    """A file's bytes at a commit (or a blob's, with no path); None when it isn't there."""
    spec = f"{rev_or_blob}:{path}" if path is not None else rev_or_blob
    res = subprocess.run(["git", "cat-file", "-p", spec], cwd=ROOT, capture_output=True)
    return res.stdout if res.returncode == 0 else None


def included(text):
    """What an approval comment's 'Includes ...' lines name: {'PR #K', ...} and lowercase commit SHAs. Only those lines
    count, so a SHA or '#N' elsewhere in the comment (the approval line's own head, a quote, prose) covers nothing."""
    out = set()
    for line in re.findall(r"^\s*[-*]?\s*includes\b:?(.*)$", text or "", re.I | re.M):
        out |= {pr_label(n) for n in re.findall(r"\bPR\s*#(\d+)", line, re.I)}
        out |= {sha.lower() for sha in re.findall(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b", line, re.I)}
    return out


def coverage(own, changes, approval=None):
    """{PR label or SHA: reason or None}: who covers each PR / unattributed commit in changes ([{'prs', 'commits'}]).
    own: the released PR ('PR #N'). approval: the PR approval that passed nh gate ({'label': 'PR #N', 'text': its
    comment}); it covers own, and the PRs and commits its 'Includes' lines name (included()). Without it, nothing is
    covered."""
    includes = included(approval["text"] if approval else "")
    named = {k for k in includes if k.startswith("PR #")}
    shas = {k for k in includes if not k.startswith("PR #")}
    result = {}
    for k in pr_sort({k for c in changes for k in c["prs"]}):
        if k == own:
            result[k] = "released PR" if approval else None
        else:
            result[k] = f"named on {own}'s approval" if approval and k in named else None
    for sha in sorted({s for c in changes for s in c["commits"]}):
        hit = approval and any(x.startswith(sha.lower()) or sha.lower().startswith(x) for x in shas)
        result[sha] = f"named on {own}'s approval" if hit else None
    return result


# --- Production compare: what a release sends against what production runs now ---

LIVE, CHANGED, NEW, UNKNOWN = "already live", "changed", "new", "not in git"


def is_object(path):
    return path.startswith("src/Objects/")


def cabinet_of(path):
    """src/FileCabinet/<x> -> /<x> (the File Cabinet path; '|' for a folder stored locally with '-' comes from the
    upload's own path, see release_compare)."""
    return path[len("src/FileCabinet"):]


def same_copy(path, a, b):
    """True when production's copy b is the same as git's copy a: files by bytes (line endings aside), objects by
    meaning (sdf_compare.object_differences, with the repo's own ignore rules)."""
    if is_object(path):
        return sdf_compare.same_object(a.decode("utf-8", "replace"), b.decode("utf-8", "replace"),
                                       CONFIG["compare"].get("ignore", ()))
    return sdf_compare.same_file(a, b)


def path_versions(path, head, first_parent):
    """[(commit, blob or None)], newest first: each commit in head's history that changed path, with path's blob after
    it. first_parent: main's own line only (what main held at each step), else every commit (a branch's versions)."""
    args = ["-c", "core.quotepath=off", "log", "--format=%x01%H", "--raw", "--no-abbrev", "--no-renames",
            "--full-history", "-m"] + (["--first-parent"] if first_parent else []) + [head, "--", path]
    out = git(*args)
    rows = []
    for block in out.split("\x01")[1:]:
        lines = block.strip().splitlines()
        if not lines:
            continue
        for line in lines[1:]:
            meta, _, name = line.partition("\t")
            if name == path and meta.startswith(":"):
                blob = meta.split()[3]
                rows.append((lines[0].strip(), None if set(blob) == {"0"} else blob))
                break
    return rows


def where_production_is(path, prod, head):
    """(commit, on main's line) of the newest version of path in head's history that production's copy matches,
    or (None, False) when git never had production's version."""
    blobs = {}
    for first_parent in (True, False):
        for commit, blob in path_versions(path, head, first_parent):
            if blob is None:
                continue
            if blob not in blobs:
                blobs[blob] = same_copy(path, show_bytes(blob) or b"", prod)
            if blobs[blob]:
                return commit, first_parent
    return None, False


def import_production(items, head, out_dir):
    """Production's current copies of items ([(src path, File Cabinet path)]): {src path: bytes, or None when
    production has none}. Read-only: imports into a temporary checkout of head, never into this checkout; every
    copy found is kept under out_dir/production/ to read. Refuses (nothing compared) when an import fails."""
    auth_id = check_env("prod")
    tmp = Path(tempfile.mkdtemp(prefix="sdf-compare-"))
    git("worktree", "add", "-q", "--detach", str(tmp), head)
    found = {}
    try:
        local = {p: tmp / local_rel(p) for p, _ in items}
        for p in local.values():
            p.unlink(missing_ok=True)   # so a file after the import is production's copy
        (tmp / "src" / "Objects").mkdir(parents=True, exist_ok=True)
        files = [c for p, c in items if not is_object(p)]
        objects = [Path(p).stem for p, _ in items if is_object(p)]
        if files:
            code, out = suitecloud(["file:import", "--paths", *files], auth_id, capture=True, env=import_env(), cwd=tmp)
            if code != 0 and not re.search(r"were imported", out):
                fail(f"could not import production's copies of the files (exit {code}); nothing was compared:\n{out.strip()[-2000:]}")
        if objects:
            code, out = suitecloud(["object:import", "--type", "ALL", "--scriptid", *objects, "--destinationfolder",
                                    "/Objects", "--excludefiles"], auth_id, capture=True, env=import_env(), cwd=tmp)
            if code != 0 and not re.search(r"were imported|objects? (?:were|was) not imported|could not be imported", out, re.I):
                fail(f"could not import production's copies of the objects (exit {code}); nothing was compared:\n{out.strip()[-2000:]}")
        cabinet = dict(items)
        missing = [p for p, f in local.items() if not f.is_file()]
        absent = confirmed_absent(missing, cabinet, auth_id, tmp)
        for p, f in local.items():
            if p in missing and p not in absent:
                fail(f"could not import production's copy of {p} (production has it, or its absence could not be "
                     "confirmed); nothing was compared. Run the dry run again; if it keeps failing, import it by hand "
                     f"({entry()} import-files/import-objects prod ... --reference) to see why")
            found[p] = f.read_bytes() if f.is_file() else None
            if found[p] is not None:
                keep = out_dir / "production" / local_rel(p)
                keep.parent.mkdir(parents=True, exist_ok=True)
                keep.write_bytes(found[p])
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(tmp)], cwd=ROOT, capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)
    return found


ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def listed_lines(out):
    """The lines of a SuiteCloud listing, without its terminal codes."""
    return [ANSI.sub("", line).strip() for line in (out or "").splitlines()]


def confirmed_absent(missing, cabinet, auth_id, cwd):
    """The src paths among missing (items an import left no copy of) that a listing confirms production doesn't
    have. Anything else (production lists it, or the listing fails) is not confirmed: the caller refuses, so a
    failed import never passes as 'new' and overwrites production's copy."""
    absent = set()
    folders = {}
    for p in missing:
        if is_object(p):
            sid = Path(p).stem.lower()
            code, out = suitecloud(["object:list", "--scriptid", Path(p).stem], auth_id, capture=True, cwd=cwd)
            lines = listed_lines(out)
            listed = {l.split(":", 1)[1].lower() for l in lines if re.fullmatch(r"[a-z_]+:[A-Za-z0-9_]+", l)}
            none = any("no objects matching" in l.lower() for l in lines)
            if code == 0 and sid not in listed and (none or listed):
                absent.add(p)
        else:
            folders.setdefault(cabinet[p].rsplit("/", 1)[0], []).append(p)
    listings = {}

    def files_in(folder):
        """The File Cabinet paths file:list shows under folder (it lists subfolders too), lowercased; empty when it
        shows none or fails ('No files found.' is also what a folder production doesn't have gives)."""
        if folder not in listings:
            code, out = suitecloud(["file:list", "--folder", folder], auth_id, capture=True, cwd=cwd)
            listings[folder] = {l.lower() for l in listed_lines(out) if l.startswith("/")} if code == 0 else set()
        return listings[folder]

    for folder, paths in folders.items():
        # Absent only by a listing that shows files: the folder's own, or (a new folder) the nearest parent's,
        # which then shows nothing under the missing folder. No listing with files up to the top: not confirmed.
        at = folder
        while not files_in(at) and at.count("/") > 1:
            at = at.rsplit("/", 1)[0]
        listed = files_in(at)
        if not listed or (at != folder and any(l.startswith(folder.lower() + "/") for l in listed)):
            continue
        absent.update(p for p in paths if cabinet[p].lower() not in listed)
    return absent


def compare_out_dir():
    """Where a compare keeps production's copies and the diffs to read: outside the repo, so never deployed."""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = Path(tempfile.gettempdir()) / "sdf-compare" / f"{ROOT.name}-{stamp}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def compare_items(items, head="HEAD", own=None, fetch=None):
    """Compare each item ([(src path, File Cabinet path)]) as it is at head with production's copy.
    Returns (out_dir, [{'path', 'state', 'prs', 'commits', 'since', 'diff'}]): state is LIVE, CHANGED (production
    has an older version git had; prs/commits changed it since), NEW (prs/commits made it) or UNKNOWN (production's
    version is not in git; diff names a file to read). fetch: import_production (tests pass a fake). own: see
    change_owners."""
    out_dir = compare_out_dir()
    prod = (fetch or import_production)(items, head, out_dir)
    cache, results = {}, []
    for path, _ in items:
        git_copy = show_bytes(head, path) or b""
        copy = prod.get(path)
        row = {"path": path, "prs": [], "commits": [], "since": None, "diff": None}
        if copy is None:
            row["state"] = NEW
            row["prs"], row["commits"] = change_owners([head], path, own, cache=cache)
        elif same_copy(path, git_copy, copy):
            row["state"] = LIVE
        else:
            since, on_main = where_production_is(path, copy, head)
            diff = out_dir / "diffs" / (local_rel(path).replace("/", "__") + ".diff")
            diff.parent.mkdir(parents=True, exist_ok=True)
            text = sdf_compare.text_diff(path, copy, git_copy, git("rev-parse", "--short=7", head).strip())
            if is_object(path):
                text = ("Differences by meaning (production -> git):\n"
                        + "\n".join("  " + d for d in sdf_compare.object_differences(git_copy.decode("utf-8", "replace"),
                                                                                     copy.decode("utf-8", "replace"),
                                                                                     CONFIG["compare"].get("ignore", ())))
                        + "\n\n" + text)
            diff.write_text(text, encoding="utf-8")
            row["diff"] = diff
            if since is None:
                row["state"] = UNKNOWN
            else:
                row["state"], row["since"] = CHANGED, since
                row["prs"], row["commits"] = change_owners([f"{since}..{head}"], path, own, cache=cache)
        results.append(row)
    return out_dir, results


def release_compare(own, rev, action, items, head="HEAD", approval=None, own_commits=None, fetch=None,
                    upload_objects=None):
    """Print what this production deploy/upload carries against production and return the refusal (or None).
    own: the released PR ('PR #N'); items: [(src path, File Cabinet path)] it sends. approval: the PR approval that
    passed nh gate (see coverage()); own_commits: see change_owners (a preview before the merge). upload_objects:
    for an upload, the src/Objects paths the PRs in it changed (a set, or a function of those PRs' labels: the
    released PR and the PRs the compare finds); any of them not live in production means it is a deploy."""
    sent = {p for p, _ in items}
    out_dir, rows = compare_items(items, head, own_commits, fetch)
    checks = []
    if upload_objects is not None:
        labels = {own} | {k for r in rows if r["state"] in (CHANGED, NEW) for k in r["prs"]}
        extra = sorted(set(upload_objects(labels) if callable(upload_objects) else upload_objects) - sent)
        if extra:
            checks = compare_items([(p, None) for p in extra], head, own_commits, fetch)[1]
    print(f"sdf: production compare for {own}'s {action} of {rev}: production's current copies, imported read-only "
          f"(kept in {out_dir})")
    owners = lambda r: ", ".join(r["prs"] + [f"commit {s} (no PR)" for s in r["commits"]]) or "no change in git"
    for state, title in ((LIVE, "Already live (sent again, needs no approval)"),
                         (CHANGED, "Changed since production's version (needs the PRs that changed it since)"),
                         (NEW, "New, not in production yet (needs the PRs that made it)"),
                         (UNKNOWN, "PRODUCTION HOLDS A CHANGE GIT DOESN'T HAVE")):
        mine = [r for r in rows if r["state"] == state]
        if not mine:
            continue
        print(f"  {title}:")
        for r in mine:
            if state == LIVE:
                print(f"    {r['path']}")
            elif state == CHANGED:
                print(f"    {r['path']}   {owners(r)}   (production has git's version of {r['since'][:7]}; diff: {r['diff']})")
            elif state == NEW:
                print(f"    {r['path']}   {owners(r)}")
            else:
                print(f"    {r['path']}   diff: {r['diff']}")
    unknown = [r for r in rows + checks if r["state"] == UNKNOWN]
    if unknown:
        lines = ["production holds a change git doesn't have:"]
        lines += [f"  {r['path']}   (read the difference, production -> git: {r['diff']})" for r in unknown]
        lines += ["A release would overwrite it. Bring production's version into git first: import it on a branch "
                  f"({entry()} import-files prod --paths <file> / import-objects prod --scriptid <id>), commit what "
                  "should stay, merge that through a PR, then release again."]
        return "\n".join(lines)
    pending = [r for r in checks if r["state"] != LIVE]
    if pending:
        return (f"the PRs in this upload also changed objects production doesn't have yet "
                f"({', '.join(r['path'] for r in pending)}). That is an object change; use deploy, not upload")
    needs = [r for r in rows if r["state"] in (CHANGED, NEW)]
    cover = coverage(own, needs, approval)
    if cover:
        print("  Needs approval: " + ", ".join(f"{k} ({v or 'NOT COVERED'})" for k, v in cover.items()))
    else:
        print("  Needs approval: nothing beyond the gates (everything it sends is already live)")
    uncovered = [k for k, v in cover.items() if v is None and k != own]
    if not uncovered:
        return None
    lines = [f"this {action} also carries changes no approval covers:"]
    for k in uncovered:
        lines.append(f"  {k}: {', '.join(r['path'] for r in needs if k in r['prs'] or k in r['commits'])}")
    names = ", ".join(uncovered)
    lines += ["Fix one of:",
              f"  - the owner approves them as part of this release: the supervisor adds 'Includes {names}' to the "
              f"approval comment on {own} (the comment holding its 'Approved by' line)",
              "  - or take those changes off main (revert through a PR) and release the new commit."]
    return "\n".join(lines)


def pr_objects(labels, head="HEAD"):
    """The src/Objects paths the merge commits of these PRs (on head's first-parent line) changed."""
    out = set()
    for full, short, parents, subject, files in first_parent_history([head]):
        m = PR_SUBJECT.search(subject)
        if len(parents) > 1 and m and pr_label(m.group(1)) in labels:
            out |= {f for f in files if is_object(f)}
    existing = set(git("ls-tree", "-r", "--name-only", head, "--", "src/Objects").splitlines())
    return out & existing


# --- Release ancestry: every earlier production release must be in this commit's history ---

def unmerged_releases(head="HEAD"):
    """prod-* tags whose commit is not an ancestor of head, oldest first: [(tag, commit, owner)].
    Without this check a release built without another release's commit would silently push older versions of
    its files."""
    out = git("for-each-ref", "--no-merged", head, "--sort=creatordate",
              "--format=%(refname:short)%00%(objectname)%00%(*objectname)%00%(contents)%01", "refs/tags/prod-*")
    tags = []
    for rec in out.split("\x01"):
        if rec.strip():
            name, obj, peeled, body = rec.lstrip("\n").split("\x00", 3)
            tags.append((name, peeled or obj, tag_owner(name, body)))
    return tags


def ancestry_refusal(head="HEAD"):
    """The refusal message when production runs releases head does not contain (else None).
    It names the commits to merge: the released commits no other missing release contains."""
    missing = unmerged_releases(head)
    if not missing:
        return None
    commits = list(dict.fromkeys(c for _, c, _ in missing))
    tips = [c for c in commits if not any(o != c and is_ancestor(c, o) for o in commits)]
    lines = ["production runs releases this commit does not contain:"]
    for name, commit, owner in missing:
        lines.append(f"  {name} ({commit[:7]}) {owner}")
    lines += [f"Releasing from here would put older versions of their files back in production. Merge the released "
              f"work into this branch first (the released commit, not its whole branch, so nothing unreleased comes along):",
              "  git fetch origin --tags"]
    for c in tips:
        owners = sorted({o for _, cc, o in missing if cc == c})
        lines.append(f"  git merge {c[:7]}    # {', '.join(owners)}")
    lines.append("Then commit, push, and run the release again (a new commit needs a new approval).")
    return "\n".join(lines)


def check_release_ancestry(action, strict=True):
    """Fetch origin's prod-* tags (another checkout may have released) and return ancestry_refusal().
    strict=False (dry run) reports a failed fetch and carries on with the local tags."""
    res = subprocess.run(["git", "fetch", "-q", "origin", "refs/tags/prod-*:refs/tags/prod-*"], cwd=ROOT,
                         capture_output=True, text=True)
    if res.returncode != 0:
        if strict:
            fail(f"could not fetch prod-* tags from origin ({res.stderr.strip()}); a production {action} needs them")
        print(f"sdf: note: could not fetch prod-* tags from origin ({res.stderr.strip()}); checking local tags only")
    return ancestry_refusal()


# --- Deploy scope: a deploy sends only one change's own files and objects ---

def changed_paths(base, tip="HEAD", head="HEAD"):
    """The src/ paths a deploy of the change base..tip sends: every file and object that differs between base and
    tip and exists at head, within src/deploy.xml, plus the script file of each of those script records. Never
    another change's work, so an old copy of it can't overwrite what is released. A file changed and put back
    exactly as it was does not differ, so it is not sent. src/manifest.xml and src/deploy.xml belong to the
    project, not a change; .attributes files go with their files."""
    names = [n for n in git("diff", "--name-only", "--no-renames", base, tip, "--", "src").splitlines() if n.strip()]
    existing = set(git("ls-tree", "-r", "--name-only", head, "--", "src").splitlines())
    patterns = deploy_patterns(head)
    paths = {p for p in names if p in existing and p not in ("src/manifest.xml", "src/deploy.xml")
             and "/.attributes/" not in p and is_deployed(p, patterns)}
    for obj in [p for p in paths if is_object(p)]:
        m = re.search(r"<scriptfile>\[(/SuiteScripts/[^\]]+)\]</scriptfile>", show_file(head, obj) or "")
        if m and f"src/FileCabinet{m.group(1)}" in existing:
            paths.add(f"src/FileCabinet{m.group(1)}")
    return sorted(paths)


def branch_scope():
    """Sandbox: what this branch changed since its merge-base with origin/main (git diff origin/main...HEAD)."""
    base = branch_base()
    paths = changed_paths(base)
    if not paths:
        fail(f"this branch changes no deployable file or object in src/ since origin/main (merge-base {base[:7]}); "
             "nothing to deploy")
    return paths


def pr_scope(pr, data):
    """(paths, compare head, own commits): what a production deploy of PR #pr sends. After the merge: the src/ files
    and objects its merge commit changed (git diff <merge>^1 <merge>), as they are at HEAD. Before the merge (a dry
    run's preview only): what the PR's head changed since its merge-base with origin/main, as at the PR's head."""
    data = data or {}
    merge = (data.get("mergeCommit") or {}).get("oid") or ""
    if data.get("state") == "MERGED" and has_commit(merge):
        refusal = not_a_merge_commit(pr, merge)
        if refusal:
            fail(refusal)
        paths, head, own = changed_paths(f"{merge}^1", merge), "HEAD", None
    else:
        tip = data.get("headRefOid") or ""
        if tip and not has_commit(tip):
            subprocess.run(["git", "fetch", "-q", "origin", f"pull/{pr}/head"], cwd=ROOT, capture_output=True)
        if not has_commit(tip):
            fail(f"PR #{pr}'s head {tip[:7] or '(unknown)'} is not in this checkout; fetch it to preview this release")
        base = git("merge-base", "origin/main" if has_commit("origin/main") else "HEAD", tip).strip()
        paths, head = changed_paths(base, tip, tip), tip
        own = {sha: pr_label(pr) for sha in git("rev-list", f"{base}..{tip}").split()}
    if not paths:
        fail(f"PR #{pr} changes no deployable file or object in src/; nothing to deploy")
    return paths, head, own


def objects_in_order(objects, root=None):
    """The objects ordered so each comes after the objects it refers to ([scriptid=...]): SuiteCloud creates them in
    deploy.xml order, and a first release fails when a record refers to one not created yet. Otherwise
    alphabetical."""
    ids = {Path(p).stem.lower(): p for p in objects}
    needs = {}
    for p in objects:
        try:
            text = ((root or ROOT) / p).read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            text = ""
        own = Path(p).stem.lower()
        needs[p] = {ids[r] for r in re.findall(r"\[scriptid=([a-z0-9_]+)", text) if r in ids and r != own}
    out, done = [], set()

    def visit(p, seen=()):
        if p in done or p in seen:
            return
        for d in sorted(needs[p]):
            visit(d, seen + (p,))
        done.add(p)
        out.append(p)
    for p in sorted(objects):
        visit(p)
    return out


def paths_deploy_xml(paths, root=None):
    """deploy.xml text that sends exactly these src/ paths (objects after the objects they refer to)."""
    files = [p for p in paths if p.startswith("src/FileCabinet/")]
    objects = objects_in_order([p for p in paths if is_object(p)], root)
    out = "<deploy>\n    <!-- Written by the SDF kit for one deploy; the committed src/deploy.xml is put back after. -->"
    if files:
        out += "\n    <files>" + "".join(f"\n        <path>~/{p[4:]}</path>" for p in files) + "\n    </files>"
    if objects:
        out += "\n    <objects>" + "".join(f"\n        <path>~/{p[4:]}</path>" for p in objects) + "\n    </objects>"
    return out + "\n</deploy>\n"


def files_only(paths):
    """True when these src/ paths hold no object. NetSuite refuses a project deploy whose deploy.xml has no <objects>
    ('The object field "objects" is missing'), so such a set goes by file:upload."""
    return not any(is_object(p) for p in paths)


# --- Tool version: a release runs main's tool or newer ---

def kit_version(home=None):
    text = ((home or KIT_HOME) / "wk" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    return m.group(1) if m else "0"


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def kit_refusal():
    """The refusal when this nearly-headless is older than the version the repo's sdf.toml requires."""
    need, have = CONFIG["requires"], kit_version()
    if version_tuple(have) >= version_tuple(need):
        return None
    return (f"this repo's {CONFIG_FILE} needs nearly-headless {need} or newer, and this laptop runs nearly-headless {have} "
            f"({KIT_HOME.as_posix()}). Update nearly-headless first (nh update), then run this again")


def tool_refusal(head="HEAD"):
    """The refusal message when origin/main has changes to the repo's tool files head does not contain (else None):
    an older copy lacks the newer guards."""
    res = subprocess.run(["git", "fetch", "-q", "origin", "main"], cwd=ROOT, capture_output=True, text=True)
    if res.returncode != 0:
        return f"could not fetch origin/main to check that the deploy tool is up to date ({res.stderr.strip()})"
    files = [f for f in CONFIG.get("tool_files") or [CONFIG_FILE]]
    missing = git("log", "--format=%h %s", f"{head}..origin/main", "--", *files).strip()
    if not missing:
        return None
    return (f"this branch's deploy tool ({', '.join(files)}) is older than main's; main has:\n"
            + "\n".join(f"  {l}" for l in missing.splitlines()) +
            "\nTake main first, then run again (in production a new commit needs a new approval):\n"
            "  git fetch origin main\n  git merge origin/main\n  git push")


def local_rel(path):
    """File Cabinet path -> local path ('|' is stored as '-', as imports do)."""
    return path.replace("|", "-")


def upload_targets(paths, cabinet=None, replace_in_place=False):
    """Check File Cabinet paths for upload and return [(cabinet path, local file)].
    Only files under the repo's own folder that exist in src/FileCabinet are allowed; with replace_in_place, other
    files under /SuiteScripts/ are allowed too (the owner's decision to replace another party's file in place,
    backed up first)."""
    cabinet = cabinet or CABINET
    folder, flag = own_folder(), replace_flag() or "replace-in-place"
    targets = []
    for path in paths:
        path = cabinet_path(path).replace("\\", "/")
        if not path.startswith(folder) or folder == "/":
            if not replace_in_place:
                fail(f"{path}: upload only takes files under {folder} (replacing another file in place needs --{flag}; "
                     "see the SDF playbook)")
            if not path.startswith("/SuiteScripts/"):
                fail(f"{path}: --{flag} only takes files under /SuiteScripts/")
        elif replace_in_place:
            fail(f"{path}: is in this repo's own folder; upload it without --{flag}")
        parts = local_rel(path).split("/")[1:]
        if any(part.strip() in ("", ".", "..") for part in parts) or any(c in path for c in "*?"):
            fail(f"{path}: give a plain file path (no '..', '.', empty segments or wildcards)")
        local = cabinet.joinpath(*parts)
        if not local.is_file():
            fail(f"{path}: not found at src/FileCabinet{path}; upload only pushes files from the project")
        if path not in (t[0] for t in targets):
            targets.append((path, local))
    return targets


def reference_dir():
    return ROOT / (CONFIG.get("reference") or "reference")


def backup_account_files(env, rev, targets):
    """Before replacing files in place: import the account's current copy of each target and keep it under
    <reference>/backups/<date>-<env>-<commit>/FileCabinet/. The import overwrites the committed local file, which
    is then restored from HEAD. Refuses (nothing uploaded) if any copy can't be taken."""
    auth_id = check_env(env)
    dest_root = reference_dir() / "backups" / f"{datetime.date.today().isoformat()}-{env}-{rev}"
    before = src_files()
    code, _ = suitecloud(["file:import", "--paths", *[path for path, _ in targets]], auth_id, env=import_env())
    saved = []
    try:
        for path, local in targets:
            if code != 0 or not local.is_file():
                fail(f"could not import the {env} copy of {path}; nothing was uploaded")
            if git("status", "--porcelain", "--", str(local)).strip() == "":
                print(f"sdf: {path}: the {env} copy is identical to HEAD")
            target = dest_root / "FileCabinet" / local.relative_to(CABINET)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local, target)
            saved.append(target.relative_to(ROOT).as_posix())
    finally:
        git("restore", "--source=HEAD", "--", *[str(local) for _, local in targets])
        # The import also brings .attributes files; keep them with the backup, out of src/.
        for extra in sorted(src_files() - before):
            target = dest_root / "FileCabinet" / extra.relative_to(CABINET)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(extra), target)
            saved.append(target.relative_to(ROOT).as_posix())
            if not any(extra.parent.iterdir()):
                extra.parent.rmdir()
    if src_dirty():
        fail("src/ is not clean after the backup import; check git status before uploading")
    for s in saved:
        print(f"  backup -> {s}")
    print(f"sdf: backed up {len(saved)} {env} file(s); commit them on the branch (restore = upload the backup)")


def only_targets(scriptids, src=None):
    """(folders, objects) for --only: deploy.xml paths of the named script records and their scripts' folders."""
    src = src or SRC
    folder = own_folder()
    files, objects = [], []
    for sid in scriptids:
        if not re.fullmatch(r"custom(script|deploy)_[a-z0-9_]+", sid):
            fail(f"--only takes script record IDs like customscript_x, not {sid!r}")
        xml = src / "Objects" / f"{sid}.xml"
        if not xml.is_file():
            fail(f"--only {sid}: no src/Objects/{sid}.xml")
        m = re.search(r"<scriptfile>\[(" + re.escape(folder) + r"[^\]]+)/[^/\]]+\]</scriptfile>", xml.read_text(encoding="utf-8"))
        if not m:
            fail(f"--only {sid}: its script file is not under {folder}")
        entry_ = f"~/FileCabinet{m.group(1)}/*"
        if entry_ not in files:
            files.append(entry_)
        objects.append(f"~/Objects/{sid}.xml")
    return files, objects


def only_paths(scriptids, src=None):
    """The src/ paths a --only deploy sends (for its sandbox turn): the records, and every file in their folders."""
    src = src or SRC
    folders, objects = only_targets(scriptids, src)
    paths = [f"src/{o[2:]}" for o in objects]
    for folder in folders:
        base = src / folder[2:-2]
        paths += [f"src/{f.relative_to(src).as_posix()}" for f in sorted(base.rglob("*"))
                  if f.is_file() and ".attributes" not in f.relative_to(base).parts]
    return sorted(dict.fromkeys(paths))


def only_deploy_xml(scriptids, src=None):
    """deploy.xml text for --only: the named script records plus every file in their scripts' folders.
    Lets a branch deploy one script to sandbox without re-pushing the rest of its work."""
    files, objects = only_targets(scriptids, src or SRC)
    body = "".join(f"\n        <path>{p}</path>" for p in files)
    objs = "".join(f"\n        <path>{p}</path>" for p in objects)
    return f"<deploy>\n    <files>{body}\n    </files>\n    <objects>{objs}\n    </objects>\n</deploy>\n"


class scoped_deploy_xml:
    """Swap src/deploy.xml for a scoped list (deploy.xml text) while a deploy runs, and always put it back."""
    def __init__(self, text, src=None):
        self.text = text
        self.path = (src or SRC) / "deploy.xml"

    def __enter__(self):
        if self.text:
            self.saved = self.path.read_bytes()
            self.path.write_text(self.text, encoding="utf-8")
        return self

    def __exit__(self, *exc):
        if self.text:
            self.path.write_bytes(self.saved)
        return False


def node_check(files):
    """Run node --check on each .js file and refuse on the first syntax error."""
    js = [f for f in files if f.suffix.lower() == ".js"]
    if not js:
        return
    node = shutil.which("node") or fail("node not found on PATH (needed for node --check)")
    for f in js:
        res = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
        if res.returncode != 0:
            fail(f"node --check failed for {f.name}:\n{res.stderr.strip()}")


def branch_base():
    """The merge-base with origin/main: a sandbox deploy sends, and a sandbox upload checks for objects, what this
    branch changed since it. Production goes by the PR's merge commit instead."""
    git("fetch", "-q", "origin", "main")
    return git("merge-base", "HEAD", "origin/main").strip()


def objects_changed(base):
    """src/Objects files that differ between base and HEAD."""
    out = git("diff", "--name-only", base, "HEAD", "--", "src/Objects")
    return [line for line in out.splitlines() if line.strip()]


def cabinet_path(path):
    """Undo Git Bash (MSYS) path conversion: /SuiteScripts -> C:/Program Files/Git/SuiteScripts."""
    m = re.match(r"^[A-Za-z]:[/\\].*?[/\\]Git(/.*)$", path.replace("\\", "/"))
    return m.group(1) if m else path


def git(*args):
    res = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode != 0:
        fail(f"git {' '.join(args)} failed: {res.stderr.strip()}")
    return res.stdout


def src_dirty():
    return git("status", "--porcelain", "--untracked-files=all", "--", "src").strip()


def src_files():
    return {p for p in SRC.rglob("*") if p.is_file()}


def move_imports_to_reference(before):
    """Move everything the import added or changed under src/ into <reference>/<today>/."""
    dest_root = reference_dir() / datetime.date.today().isoformat()
    changed = [ROOT / line[3:].strip().strip('"') for line in git("status", "--porcelain", "--", "src").splitlines() if line[:2].strip() == "M"]
    added = sorted(src_files() - before)
    moved = []
    for path in added + changed:
        rel = path.relative_to(SRC)
        target = dest_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if path in changed:
            shutil.copy2(path, target)
        else:
            shutil.move(str(path), target)
        moved.append(target.relative_to(ROOT).as_posix())
    if changed:
        git("restore", "--", *[str(p) for p in changed])
    # Drop directories the import left empty (keep the project skeleton).
    keep = {SRC / "Objects"} | ({CABINET / own_folder().strip("/")} if own_folder() != "/" else set())
    for d in sorted((p for p in SRC.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        if d not in keep and not any(d.iterdir()) and not any(k.is_relative_to(d) for k in keep):
            d.rmdir()
    for m in moved:
        print(f"  -> {m}")
    print(f"sdf: {len(moved)} file(s) stored under {dest_root.relative_to(ROOT).as_posix()}/ (evidence only; never deployed)")


def run_import(env, cli_args, reference):
    auth_id = check_env(env)
    if reference:
        if src_dirty():
            fail("src/ has uncommitted changes; commit or clean them before a --reference import")
        before = src_files()
    (SRC / "Objects").mkdir(exist_ok=True)  # object:import needs it; git does not keep empty folders
    code, _ = suitecloud(cli_args, auth_id, env=import_env())
    if reference:
        move_imports_to_reference(before)
    return code


def release_sandbox(dryrun=False):
    """Put main's version of every file this branch's sandbox turn holds back in the sandbox, then free
    the turn, so the next builder tests on main plus its own work. Runs from a temporary checkout of origin/main.
    A file main does not have stays in the sandbox as this branch sent it (deploys never delete); it is listed.
    Files a project deploy does not send (files replaced in place, held as their File Cabinet path) go back by
    upload. If NetSuite refuses, the turn is kept."""
    items = held_items()
    if not items:
        fail(f"this branch holds no {SANDBOX} turn; nothing to release")
    git("fetch", "-q", "origin", "main")
    on_main = lambda p: subprocess.run(["git", "cat-file", "-e", f"origin/main:{local_rel(p)}"], cwd=ROOT,
                                       capture_output=True).returncode == 0
    keep = [p for p in items if on_main(p)]
    gone = [p for p in items if p not in keep]
    patterns = deploy_patterns("origin/main")
    uploads = [p for p in keep if p.startswith("src/FileCabinet/") and ("|" in p or not is_deployed(p, patterns))]
    deploys = [p for p in keep if p not in uploads]
    if files_only(deploys):
        uploads, deploys = deploys + uploads, []
    print(f"sdf: returning {len(keep)} file(s) to main's version ({git('rev-parse', '--short=7', 'origin/main').strip()}) "
          f"in the sandbox, then freeing the turn")
    for p in deploys:
        print(f"  deploy {p}")
    for p in uploads:
        print(f"  upload {p[len('src/FileCabinet'):]}")
    for p in gone:
        print(f"  not on main, stays in the sandbox as this branch sent it (remove by hand if it must go): {p}")
    if dryrun:
        print("sdf: dry run: nothing sent, the turn is kept")
        return 0
    if keep:
        auth_id = check_env("sb")
        tmp = Path(tempfile.mkdtemp(prefix="sdf-release-"))
        git("worktree", "add", "-q", "--detach", str(tmp), "origin/main")
        try:
            code = 0
            if deploys:
                with scoped_deploy_xml(paths_deploy_xml(deploys, tmp), tmp / "src"):
                    code = suitecloud(["project:deploy", "--validate"], auth_id, cwd=tmp)[0]
            if code == 0 and uploads:
                cab = [p[len("src/FileCabinet"):] for p in uploads]
                code = suitecloud(["file:upload", "--paths", *cab], auth_id, cwd=tmp,
                                  env=import_env() if any("|" in p for p in cab) else None)[0]
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(tmp)], cwd=ROOT, capture_output=True)
            shutil.rmtree(tmp, ignore_errors=True)
        if code != 0:
            fail(f"NetSuite refused (exit {code}); the sandbox may still hold this branch's files, so the turn is kept. "
                 "Fix and run release sb again")
    refusal = wk_refusal("turn", "release", SANDBOX, *items)
    if refusal:
        fail(refusal)
    print(f"sdf: {SANDBOX} turn released: " + ", ".join(items))
    return 0


def login(env):
    """Create the env's auth ID with the browser flow pinned to the account's own domain (sdf.toml)."""
    auth_id, domain = auth_ids()[env], env_cfg(env).get("domain") or ""
    if not domain:
        fail(f"{CONFIG_FILE}: [{env}] needs domain (the account's login domain) for login")
    if auth_info(auth_id) is not None:
        fail(f"{auth_id} already exists; if it is expired or wrong, first run: suitecloud account:manageauth --remove {auth_id}")
    node = shutil.which("node") or fail("node not found on PATH")
    print(f"sdf: starting browser login for {auth_id} at {domain}. Log in there, pick the role, click Allow.", flush=True)
    code = subprocess.run([node, str(KIT / "sdf_auth.js"), auth_id, domain], cwd=ROOT).returncode
    if code != 0:
        fail(f"login for {auth_id} did not complete (exit {code})")
    check_env(env)  # refuses, and says so, if the login landed on the wrong account
    print(f"sdf: {auth_id} verified: {auth_info(auth_id)}")
    return 0


def deploy_prod(a, rev):
    """deploy prod --pr N [--dryrun]: the gates, the PR's scope, release ancestry, then the production compare."""
    gates, approval, data = prod_gates("deploy", a.pr)
    if a.dryrun:  # a preview: the gates, and the production compare (production read-only); nothing is sent or tagged
        if not a.pr:
            fail("\n".join(gates))
        scope, head, own = pr_scope(a.pr, data)
        ancestry = check_release_ancestry("deploy", strict=False)
        refusal = release_compare(pr_label(a.pr), rev, "deploy", [(p, cabinet_of(p)) for p in scope], head=head,
                                  approval=approval, own_commits=own)
        refusals = [r for r in [ancestry, refusal, *gates] if r]
        print("sdf: dry run: a real deploy would " + ("REFUSE: " + "\nand ".join(refusals) if refusals
              else "pass the gates and the production compare") + ". For a server-side check use: validate prod --server")
        return None, 1 if refusals else 0
    if gates:
        fail("\n".join(gates))
    print(f"sdf: on main, level with origin/main; PR #{a.pr} merged and approved for {prod_action('deploy')}")
    scope, _, _ = pr_scope(a.pr, data)
    refusal = check_release_ancestry("deploy")
    if refusal:
        fail(refusal)
    refusal = release_compare(pr_label(a.pr), rev, "deploy", [(p, cabinet_of(p)) for p in scope], approval=approval)
    if refusal:
        fail(refusal)
    return scope, None


def main(argv=None, root=None):
    """root: the client repo (its entry point passes it); default: the nearest folder up from here with sdf.toml."""
    configure(root or find_root())
    ids = auth_ids()
    flag = replace_flag()
    p = argparse.ArgumentParser(prog=entry(), description=f"SDF kit (auth IDs {ids['sb']} / {ids['prod']})")
    sub = p.add_subparsers(dest="cmd", required=True)
    envs = ("sb", "prod")

    sub.add_parser("auth", help=f"show {ids['sb']} / {ids['prod']} auth status")

    s = sub.add_parser("login", help="create an auth ID via browser login (the owner clicks Allow)")
    s.add_argument("env", choices=envs)

    s = sub.add_parser("list-objects", help="list custom objects in the account")
    s.add_argument("env", choices=envs)
    s.add_argument("--type", nargs="+")
    s.add_argument("--scriptid")

    s = sub.add_parser("list-files", help="list File Cabinet files")
    s.add_argument("env", choices=envs)
    s.add_argument("--folder", default="/SuiteScripts")

    s = sub.add_parser("import-objects", help="import custom objects (and their script files)")
    s.add_argument("env", choices=envs)
    s.add_argument("--type", default="ALL")
    s.add_argument("--scriptid", nargs="+", required=True)
    s.add_argument("--excludefiles", action="store_true", help="do not import referenced script files")
    s.add_argument("--reference", action="store_true", help="evidence: store under <reference>/<date>/")

    s = sub.add_parser("import-files", help="import File Cabinet files")
    s.add_argument("env", choices=envs)
    s.add_argument("--paths", nargs="+", required=True)
    s.add_argument("--reference", action="store_true", help="evidence: store under <reference>/<date>/")

    s = sub.add_parser("validate", help="validate src/ against an account")
    s.add_argument("env", choices=envs)
    s.add_argument("--server", action="store_true", help="server-side validation")

    s = sub.add_parser("deploy", help="deploy src/ (sandbox: this branch's changes, needs a turn; production: --pr N from main)")
    s.add_argument("env", choices=envs)
    s.add_argument("--pr", type=int, help="production: the merged PR to release (its merge commit's changes)")
    s.add_argument("--dryrun", action="store_true")
    s.add_argument("--only", nargs="+", metavar="SCRIPTID",
                   help="sandbox: deploy only these script records and the files in their scripts' folders "
                        "(without it, a deploy sends what this branch changed since origin/main)")

    s = sub.add_parser("upload", help=f"code-only: upload named files under {own_folder()} (as deploy: turns, gates)")
    s.add_argument("env", choices=envs)
    s.add_argument("--pr", type=int, help="production: the merged PR whose approval covers this upload")
    s.add_argument("--paths", nargs="+", required=True, help=f"File Cabinet paths, e.g. {own_folder()}area/x.js")
    if flag:
        s.add_argument(f"--{flag}", dest="replace_in_place", action="store_true",
                       help="overwrite files outside this repo's folder in place; backs up the account copy first")

    s = sub.add_parser("release", help="sandbox: put main's version of this branch's turn files back, then free the turn")
    s.add_argument("env", choices=("sb",))
    s.add_argument("--dryrun", action="store_true", help="list what it would send; send nothing, keep the turn")

    a = p.parse_args(argv)
    replace_in_place = getattr(a, "replace_in_place", False)

    refusal = kit_refusal()
    if refusal:
        fail(refusal)

    if a.cmd == "release":
        return release_sandbox(a.dryrun)

    if a.cmd == "auth":
        for env, auth_id in ids.items():
            info = auth_info(auth_id)
            print(f"{auth_id:9} " + (json.dumps(info) if info else "NOT SET UP"))
        return 0

    if a.cmd == "login":
        return login(a.env)

    if a.cmd == "list-objects":
        args = ["object:list"] + (["--type", *a.type] if a.type else []) + (["--scriptid", a.scriptid] if a.scriptid else [])
        return suitecloud(args, check_env(a.env))[0]

    if a.cmd == "list-files":
        return suitecloud(["file:list", "--folder", cabinet_path(a.folder)], check_env(a.env))[0]

    if a.cmd == "import-objects":
        args = ["object:import", "--type", a.type, "--scriptid", *a.scriptid, "--destinationfolder", "/Objects"]
        if a.excludefiles:
            args.append("--excludefiles")
        return run_import(a.env, args, a.reference)

    if a.cmd == "import-files":
        return run_import(a.env, ["file:import", "--paths", *[cabinet_path(x) for x in a.paths]], a.reference)

    if a.cmd == "validate":
        return suitecloud(["project:validate"] + (["--server"] if a.server else []), check_env(a.env))[0]

    if a.cmd in ("deploy", "upload"):
        if a.pr and a.env == "sb":
            fail("--pr is for production; a sandbox deploy sends what this branch changed since origin/main")
        if src_dirty():
            fail(f"src/ has uncommitted changes; commit first so the {a.cmd} matches a git revision")
        rev = git("rev-parse", "--short=7", "HEAD").strip()
        refusal = tool_refusal()
        if refusal:
            fail(refusal)

    if a.cmd == "upload":
        targets = upload_targets(a.paths, replace_in_place=replace_in_place)
        if replace_in_place:
            untracked = [str(local) for _, local in targets if not git("ls-files", "--", str(local)).strip()]
            if untracked:
                fail(f"not committed: {', '.join(untracked)}")
        node_check([local for _, local in targets])
        paths = [path for path, _ in targets]
        src_paths = ["src/FileCabinet" + local_rel(path) for path in paths]
        if a.env == "prod":
            approval, data = check_prod_gates("upload", a.pr)
            refusal = check_release_ancestry("upload")
            if refusal:
                fail(refusal)
            refusal = release_compare(pr_label(a.pr), rev, "upload", list(zip(src_paths, paths)), approval=approval,
                                      upload_objects=pr_objects)
            if refusal:
                fail(refusal)
        else:
            changed = objects_changed(branch_base())
            if changed:
                fail(f"src/Objects changed on this branch: {', '.join(changed)}. That is an object change; use deploy, not upload")
            check_turn(["src/FileCabinet" + path for path in paths])  # File Cabinet path: '|' kept
        if replace_in_place:
            backup_account_files(a.env, rev, targets)
        auth_id = check_env(a.env)
        print(f"sdf: uploading {len(paths)} file(s) at {rev} to {auth_id}: {', '.join(paths)}")
        code = suitecloud(["file:upload", "--paths", *paths], auth_id,
                          env=import_env() if any("|" in p for p in paths) else None)[0]
        if a.env == "prod" and code == 0:
            tag_release(a.pr, rev, "upload", src_paths)
        return code

    if a.cmd == "deploy":
        if a.only and a.env == "prod":
            fail("--only is for sandbox; a production deploy sends the PR's own files and objects")
        if a.env == "prod":
            scope, code = deploy_prod(a, rev)
            if scope is None:
                return code
            owner = f"PR #{a.pr}'s"
        else:
            scope = None if a.only else branch_scope()
            owner = "this branch's"
            if not a.dryrun:  # a dry run changes nothing in the sandbox, so it needs no turn
                check_turn(only_paths(a.only) if a.only else scope)
        auth_id = check_env(a.env)
        if a.only:
            what, text = ", ".join(a.only) + " (and their folders)", only_deploy_xml(a.only)
        else:
            what, text = f"{owner} own {len(scope)} file(s) and object(s)", paths_deploy_xml(scope)
            print(f"sdf: {owner} changes: " + ", ".join(scope))
        if scope is not None and files_only(scope):
            paths = [cabinet_of(p) for p in scope]
            if a.dryrun:
                print(f"sdf: dry run: {owner} changes are files only, so a real deploy uploads them (file:upload has no "
                      "dry run): " + ", ".join(paths))
                return 0
            print(f"sdf: uploading {what} at {rev} to {auth_id} (files only, so file:upload)")
            code = suitecloud(["file:upload", "--paths", *paths], auth_id)[0]
            if a.env == "prod" and code == 0:
                tag_release(a.pr, rev, "deploy", scope)
            return code
        print(f"sdf: deploying {what} at {rev} to {auth_id}" + (" (dry run)" if a.dryrun else ""))
        with scoped_deploy_xml(text):
            code = suitecloud(["project:deploy", "--dryrun" if a.dryrun else "--validate"], auth_id)[0]
        if a.env == "prod" and code == 0:
            tag_release(a.pr, rev, "deploy", scope)  # the tag lists what went out
        return code


if __name__ == "__main__":
    sys.exit(main() or 0)
