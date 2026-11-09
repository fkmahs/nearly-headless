# Install nearly-headless

From a laptop with nothing on it to a green `nh doctor` and the command center, on Windows, macOS or Linux. The kit's code is public: cloning it needs no GitHub account or login. Do the steps in order; each ends with how you know it worked.

Moving your own work from another laptop? Do this first, then [Moving your own laptop](new-laptop.md).

## 1. Install the tools

You need Python 3.11 or newer, git, and the GitHub CLI (`gh`, which supervisors use for pull requests). Docker Desktop is needed on Windows and macOS for builders' tests.

| | Windows (PowerShell) | macOS (Terminal, with [Homebrew](https://brew.sh)) | Linux (Debian or Ubuntu) |
|---|---|---|---|
| Python, git, gh | `winget install Python.Python.3.13`, `winget install Git.Git`, `winget install GitHub.cli` | `brew install python git gh` | `sudo apt update`, then `sudo apt install python3 git gh` |
| Docker | `winget install Docker.DockerDesktop` | `brew install --cask docker` | not needed: tests run directly |

**Linux with an older Python.** `nh` needs Python 3.11 or newer. Debian 12 and Ubuntu 24.04 ship it as `python3`; Ubuntu 22.04 and Debian 11 ship an older one. Keep the system's `python3` as it is and install a newer one beside it (on Ubuntu: `sudo add-apt-repository ppa:deadsnakes/ppa`, then `sudo apt install python3.12`): `nh` uses `python3.13`, `python3.12` or `python3.11` when `python3` is older. On a container or server without `sudo`, run the same commands as root without it.

On Windows and macOS, `nh test` runs the tests only in Linux, in Docker: minutes instead of tens of minutes. If Docker is off, `nh test` starts Docker Desktop and waits for it; if it can't, no tests run and the builder asks you to start Docker. Tip: in Docker Desktop's settings, turn on "Start Docker Desktop when you sign in". Docker Desktop has its own licence terms, which you accept when you first open it.

Open a new terminal. **Works when** `python3 --version` (Windows: `py --version`; Linux with an older system Python: `python3.12 --version`) shows 3.11 or newer, and `git --version`, `gh --version` and, where you installed it, `docker info` answer.

## 2. Install Claude (and Codex, if you use it)

Install the Claude desktop app and sign in; install Codex too if you use it. Do this before step 4, so setup finds them. Your work runs in the app's **Code** tab: supervisors are chats there, each opened on its repo's folder.

**On Linux**, where there is no Claude desktop app, install Claude Code, the terminal version, from Anthropic's Claude Code setup guide, and run each supervisor with `claude` in its repo's folder; or use Codex. Without either, `nh` itself still works (lists, `nh check`, `nh serve`); setup still installs the skills and hooks for when Claude arrives, and `nh doctor` shows its Claude lines as warnings.

Optional, for working from your phone: install the Claude app there with the same account and allow its notifications. In the desktop app, Settings, Claude Code: **Connect new sessions to Remote Control: On**, and **Keep computer awake while Claude works: On** (builders stop when the laptop sleeps).

## 3. Get nearly-headless

Pick a folder for it (`~/nearly-headless` below; on Windows, `C:\Projects\nearly-headless` reads well):

```
git clone https://github.com/fkmahs/nearly-headless.git ~/nearly-headless
```

**Works when** `~/nearly-headless/bin/nh version` (run it as it is, not through `sh`; Windows: `C:\Projects\nearly-headless\bin\nh.cmd version`) prints a version.

## 4. Run setup once

Give your name, and one `--repo` for each repo you work in (none is fine to start). `client=` is the name the command center shows; `url=` clones the repo if its folder doesn't exist yet.

```
~/nearly-headless/bin/nh setup --owner "Your Name" --repo "app=$HOME/code/app,client=Client name,url=https://github.com/<account>/app.git"
```

(Windows: `C:\Projects\nearly-headless\bin\nh.cmd setup ...`, with Windows paths.)

It writes this laptop's settings file (`~/.nearly-headless/settings.toml`), clones the repos, installs the skills and the instructions every chat reads for Claude (and Codex), and adds to `~/.claude/settings.json`, keeping what is there: the hook that gives a supervisor its list when its chat starts, resumes or is compacted or cleared, the reply-check hooks (off until you list parts, below), a compact window of 256,000 tokens (left as it is if you set your own), and Claude's push to your phone (left off if you turned it off). **Works when** it prints `"created": true`.

- **Updates.** A new laptop doesn't update itself: `nh update` moves it to the newest release when you choose (setup's `"auto_update"` line says so). To have it check GitHub once a day and update on its own, add `--auto-update` to setup, now or on any later run; `auto_update = false` in the settings file turns it off again.

- **Your own instructions.** Every chat reads the kit's neutral default, `templates/instructions.md`, which describes how the kit's work flows. To use your own, write them in a file of yours, add `instructions = "<that file>"` to the settings file above its first `[table]`, and run setup again.
- If it says expectations `"installed": false`, you already had your own `~/.claude/CLAUDE.md`: setup never overwrites it. Add the line it shows to that file, and copy in the section from `templates/compact-instructions.md`.
- If it says hook `"installed": false`, your `~/.claude/settings.json` couldn't be read: fix it, then run setup again.
- Setup never overwrites the settings file. To add a repo later, run `nh setup --repo "..."` the same way: it adds any repo the file lacks, clones it if its folder is missing, and lists what it added (`"added"`) and what was already there (`"already_there"`). To change a repo, edit that file.

The repos you work in need their own access if they are private: `gh auth login` (GitHub.com, then a web browser) lets both `gh` and `git` reach them. Tell git who you are, so your commits carry your name: `git config --global user.name "Your Name"` and `git config --global user.email "<your email>"`.

## 5. Put `nh` on your PATH

Run `~/nearly-headless/bin/nh doctor` (Windows: `...\bin\nh.cmd doctor`). If `nh` isn't on your PATH, its PATH line gives the one command that adds it, for you to run; then open a new terminal. **Works when** `nh version` prints a version from any folder.

Setup records the checkout it ran from as `kit` in the settings file. If `nh` on your PATH is another checkout's `bin`, it forwards there, so a kit that moves never needs a PATH change.

## 6. Check with `nh doctor`

Every line should start with `ok` (a `warn` line is advice). For a `FIX` line:

| It says | Do |
|---|---|
| settings file, or owner | step 4 |
| GitHub CLI (gh) | step 1 |
| Docker | start Docker Desktop (step 1) |
| a skill, your instructions, the recall or reply-check hook, phone push, or compacting long chats | `nh setup` again (it keeps your settings) |
| Repo '...' is not at ... | clone it there, or fix its path in the settings file |
| main checkout on main (found '...') | in that repo's folder, switch to the branch it names: `git switch main`. A repo whose default branch isn't main (`master`, say) adds `base = "master"` to its `[repos.<name>]` entry in the settings file |
| CI follows the nearly-headless template | the repo's own CI needs a fix: a piece for its supervisor, not a laptop problem |

## 7. Open the command center

`nh serve`, then open http://127.0.0.1:8765. Keep that terminal open while you use it. **Works when** your repos show, and the bottom left shows the kit's version as current.

`nh update` moves it to the newest release over https, with no login. A release is a merge on the public repo's main, so nothing unreleased arrives. With `auto_update = true` (setup's `--auto-update`), the first `nh` command each day does that on its own: it runs the new release's setup, which installs its skills and hooks, so turn it on only if you trust what lands on that repo's main.

**Agents on a laptop without push rights to the kit treat it as read-only:** they never commit to it or open PRs there. When one wants a change to nearly-headless, it writes the issue text (a plain title, What was wrong, what it would change) to a file and tells you where, for you to file on the public repo.

## Options

### Checking supervisor replies

Setup adds a hook that checks each supervisor reply before it reaches you: if a part you require is missing (each is a phrase, case ignored, such as `Since you wrote:`), Claude is sent back once to rewrite its closing message. It only checks chats that loaded the supervisor skill (never builders) and lets anything through on an error. It is off until you list the parts in `~/.nearly-headless/settings.toml`:

```toml
[reply]
require = ["Since you wrote:"]
```

A second hook (`UserPromptSubmit`, `nh reply-check --remind`) names those parts in one short line before each turn, so Claude gets the reply right the first time. Both read the same list; empty it to turn them off.

Supervisors are also held to recording support work, unless you add `record = false` under `[reply]`: a turn that drafted a message for someone, investigated or reviewed a document for you, but recorded nothing on the list (`nh piece add`, or `nh piece note ... --add`), is sent back once to record it. Each check adds one line to `~/.nearly-headless/reply-check.log`.

### A WWID profile repo

The wwid skill predicts how you would answer, from a private profile repo of your own (how you decide, and your verdicts on its predictions). Without one, it is off and everything else works. With one: clone it and run `nh setup --profile <its folder>`, which adds `wwid_profile` and the reply check's `"What would I do"` mark to the settings file once (setup also finds a `wwid` checkout beside nearly-headless by itself).

### Keeping a list apart from its code

A repo's list (`.work/`) can live in a different checkout from its code, for example a public repo whose list stays private. In the settings file:

```toml
[repos.app]
path = "/home/you/code/app-public"   # the code: PRs, CI, turns, gates
list = "/home/you/code/app-list"     # the checkout whose .work/ holds the list (its base: list_base, default main)
public = true                         # anyone can read it: nh setup adds the privacy guard to its pushes
```

Add names only you know to the guard's list under `[privacy] terms = ["..."]` in the same file, and put that list on GitHub with `nh privacy secret <account>/<repo>`, so the repo's own checks use it too. `nh doctor` shows where each list is kept, whether each public checkout has the guard, and whether GitHub's list still matches this laptop's.
