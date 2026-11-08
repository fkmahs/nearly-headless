# New laptop

From a fresh Windows laptop to a green `wk doctor` and the command center. Do the steps in order; each ends with how you know it worked. `<you>` is your GitHub account, `<repo>` a repo you work in.

## 1. Install the tools

In PowerShell, one line at a time:

```
winget install Python.Python.3.13
winget install Git.Git
winget install GitHub.cli
```

Required for builders' tests: Docker Desktop, which brings WSL2 with it (on a Mac: `brew install --cask docker`).

```
winget install Docker.DockerDesktop
```

On Windows and macOS, `wk test` runs the tests only in Linux, in Docker: minutes instead of tens of minutes. If Docker is off, `wk test` starts Docker Desktop and waits for it; if it can't, no tests run and the builder asks you to start Docker. Tip: in Docker Desktop's settings, turn on "Start Docker Desktop when you sign in", so it is always up. Docker Desktop has its own licence terms, which you accept when you first open it. That is your call.

Close PowerShell and open it again. **Works when** `py --version` shows 3.11 or newer, and `git --version`, `gh --version` and, with Docker Desktop open, `docker info` answer.

## 2. Sign in to GitHub

`gh auth login`, then choose: GitHub.com, SSH, generate a new SSH key (and upload it), log in with a web browser. This one sign-in lets both `gh` and `git` reach your private repos. If git later asks whether to trust github.com, type `yes`.

Then tell git who you are, so your commits carry your name (use your GitHub account's email, or its noreply address from GitHub's email settings):

```
git config --global user.name "Your Name"
git config --global user.email "<your email>"
```

**Works when** `gh auth status` says "Logged in" and `git config user.email` prints your email.

## 3. Install Claude

1. Install the Claude desktop app and sign in. Install Codex too if you use it. Both before step 5, so setup finds them.
2. On your phone, install the Claude app and sign in with the same account, to follow and start work from there. In the phone's settings, allow the Claude app's notifications: setup (step 5) turns on Claude's push to your phone, but can't reach the phone itself.
3. In the desktop app, Settings, Claude Code:
   - **Connect new sessions to Remote Control: On.** Lets you reach your chats from the phone.
   - **Keep computer awake while Claude works: On**, and **Keep awake on battery: On.** Builders stop when the laptop sleeps. A closed lid still sleeps.
   - **Worktree location: leave the default** (inside the project). Each builder works in its own copy of the repo there.

Your work runs in the **Code** tab: supervisors are chats there, each opened on its repo's folder.

## 4. Get work-kit

```
gh repo clone <you>/work-kit C:\Projects\work-kit
```

## 5. Run setup once

One `--repo` for each repo you work in. `client=` is the name the command center shows; `url=` clones the repo if its folder doesn't exist yet. Your old laptop's `~/.workkit/settings.toml` lists them.

```
C:\Projects\work-kit\bin\wk.cmd setup --owner "Your Name" --repo "<repo>=C:/Projects/<repo>,client=Client name,url=git@github.com:<you>/<repo>.git" --repo "..."
```

It writes this laptop's settings file (`~/.workkit/settings.toml`), clones the repos, and installs the skills and your expectations for Claude (and Codex), and adds the hook that gives a supervisor its list when its chat starts or resumes and after Claude compacts its memory (or you clear the chat), has Claude compact a chat by 256,000 tokens at most (`autoCompactWindow`, left as it is if you set your own) so no chat runs away re-reading a huge history every turn, and turns on Claude's push to your phone ("Push when Claude decides", left off if you turned it off). Both go in `~/.claude/settings.json`, keeping what is there. **Works when** it prints `"created": true`.

- If it says expectations `"installed": false`, you already had your own `~/.claude/CLAUDE.md`: add the line it shows to that file, and copy in the section from `templates/compact-instructions.md`.
- If it says hook `"installed": false`, your `~/.claude/settings.json` couldn't be read: fix it, then run setup again.
- Setup never overwrites the settings file. To add a repo later, run `wk setup --repo "..."` the same way: it adds any repo the file lacks at its end, keeping everything else, clones it if its folder is missing, and lists what it added (`"added"`) and what was already there (`"already_there"`, left unchanged). To change a repo, edit that file.

### Checking supervisor replies

Setup also adds a hook that checks each supervisor reply before it reaches you: if a part you require is missing (each is a phrase, case ignored, such as `Since you wrote:`), Claude is sent back once to rewrite its closing message. It only checks chats that loaded the supervisor skill (never builders) and lets anything through on an error. It is off until you list the parts in `~/.workkit/settings.toml`:

```toml
[reply]
require = ["Since you wrote:"]
```

Setup keeps the WWID heading, `"What would I do"`, in that list for you: it adds it on a laptop with your WWID profile repo (below), and takes it out on one without, where the wwid skill is off and writes no such section.

A second hook (`UserPromptSubmit`, `wk reply-check --remind`) names those parts in one short line before each turn, so Claude gets the reply right the first time; the check stays as the safety net. Both read the same list. Delete those lines (or empty the list) to turn them off; it takes effect on the next reply.

Supervisors are also held to recording support work, unless you add `record = false` under `[reply]`: a turn that drafted a message or email for someone, investigated (a web search, a web page, a research agent) or reviewed a document for you, but ran no `wk piece add` and no `wk piece note ... --add` (or `--file`), itself or through a subagent, is sent back once to record it, so the work reaches the invoice. A refused write or `--help` doesn't count; a turn that only revises a draft passes when the turn before it recorded one. Answers about the list or work-kit and the supervisor's own duties (the list, starting and reviewing builders, merging, releasing, checking local or test sites) pass. It reads only that turn's tool calls and, for a draft, your ask and Claude's text. It checks that something was recorded, not where: a line on an unrelated piece passes, which the supervisor skill forbids. Each check adds one line to `~/.workkit/reply-check.log` (time, chat, `ok` or what was missing), so you can see how often it fired.

### Optional: your WWID profile repo

If you keep a WWID profile for the wwid skill (how you decide, and your verdicts on its predictions), clone that private repo beside work-kit and give it to setup. Skip this on a laptop without it, such as a work laptop: everything else works, and `wk doctor` shows "WWID profile: not set (optional)".

```
gh repo clone <you>/wwid C:\Projects\wwid
C:\Projects\work-kit\bin\wk.cmd setup --profile C:/Projects/wwid
```

It adds `wwid_profile = "C:/Projects/wwid"` and the `"What would I do"` mark to the settings file, once (setup also adds a `wwid` checkout it finds beside work-kit by itself). **Works when** `wk doctor` shows `ok   WWID profile at C:/Projects/wwid`. Supervisors then end each reply with its section and record your verdicts there, without client names.

## 6. Put `wk` on your PATH

Start menu, "Edit environment variables for your account", Path, Edit, New: `C:\Projects\work-kit\bin`. OK, then open a new PowerShell. **Works when** `wk version` prints a version.

Setup records the checkout it ran from as `kit` in the settings file. If `wk` on your PATH is another checkout's `bin`, it forwards there, so a kit that moves never needs a PATH change; `wk doctor`'s first lines say which checkout ran and what forwarded it.

## 7. Restore each repo's secrets (yours to do)

Secrets never live in a repo or in work-kit. For each repo:

1. Open its README's setup section: it names the `.env` it needs.
2. Restore that `.env` from your password manager into the repo's main folder (`C:/Projects/<repo>`). One `.env` there serves all its worktrees.
3. If the repo's AGENTS.md says it uses the NetSuite toolkit, follow the `.env` part of [its setup guide](../toolkits/netsuite/README.md). The NetSuite side is done once per account and already exists. Check it with `py C:\Projects\work-kit\toolkits\netsuite\query.py --env sb --check` from the repo's folder.

If you bill with the [invoicing toolkit](../toolkits/invoicing/README.md), restore your billing profile to `%USERPROFILE%\.wk\billing-profile.json` too.

## 8. Check with `wk doctor`

Every line should start with `ok`. For a `FIX` line:

| It says | Do |
|---|---|
| settings file, or owner | step 5 |
| GitHub CLI (gh) | step 1 |
| a skill, your expectations, the recall or reply-check hook, phone push, or compacting long chats | `wk setup` again (it keeps your settings) |
| Repo '...' is not at ... | clone it there, or fix its path in the settings file |
| main checkout on main (found '...') | in that repo's folder, `git switch main` |
| CI follows the work-kit template | the repo's own CI needs a fix: a piece for its supervisor, not a laptop problem |

## 9. Open the command center

`wk serve`, then open http://127.0.0.1:8765. Keep that window open while you use it. **Works when** your repos show, and the bottom left shows work-kit's version as current. After `wk update` it restarts on the new version by itself, on the same address; if that fails it keeps running the old one and the page says so.

## 10. Restart your supervisors

Chats stay on the old laptop. For each list on the command center, open a new chat in the Code tab on that repo and say "Resume as supervisor of `<product>`". It rereads the list from the repo and links itself on the command center. Unfinished pieces restart from their PR and note.

## A read-only laptop

A laptop that uses work-kit but never changes it (a work laptop, say) needs no GitHub login for the kit: work-kit's code is public. Do steps 1 and 3, then:

```
git clone https://github.com/fkmahs/nearly-headless.git C:\Projects\nearly-headless
C:\Projects\nearly-headless\bin\wk.cmd setup --owner "Your Name" --repo "<repo>=C:/Projects/<repo>"
```

Then step 6 with `C:\Projects\nearly-headless\bin`, and steps 8 and 9. With no WWID profile, no list of work-kit's own and no login, `wk doctor` still passes. It updates itself once a day from the public repo's releases, over https, with no login: a release is a merge on its main after the owner's yes, so nothing unapproved arrives. The repos you work in still need their own access (step 2) if they are private.

**Agents on it treat the kit as read-only:** they never commit to it or open PRs there (there is no push right). When one wants a change to work-kit, it writes the issue text (a plain title, What was wrong, what it would change) to a file and tells you where; you file it on the public repo from a laptop with a login.

## Keeping a list apart from its code

A repo's list (`.work/`) can live in a different checkout from its code, for example a public repo whose list stays private. In the settings file:

```toml
[repos.app]
path = "C:/Projects/app-public"   # the code: PRs, CI, turns, gates
list = "C:/Projects/app-list"     # the checkout whose .work/ holds the list (its base: list_base, default main)
public = true                     # anyone can read it: wk setup adds the privacy guard to its pushes
```

Add names only you know to the guard's list under `[privacy] terms = ["..."]` in the same file. `wk doctor` shows where each list is kept and whether each public checkout has the guard.
