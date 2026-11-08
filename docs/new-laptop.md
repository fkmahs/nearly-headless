# Moving your own laptop

Your work, moved from one laptop to another: the same settings, secrets and lists. First install nearly-headless on the new laptop ([Install nearly-headless](install.md), steps 1 to 3). Then, in order:

## 1. Sign in to GitHub

`gh auth login` (GitHub.com, a web browser), so `gh` and `git` reach your private repos. Then `git config --global user.name "Your Name"` and `git config --global user.email "<your email>"`. **Works when** `gh auth status` says "Logged in".

If you change nearly-headless itself (you have push rights to it), clone it with that login instead of over https, so your builders can push: `gh repo clone <account>/nearly-headless <folder>`.

## 2. Bring your settings

Copy `~/.nearly-headless/settings.toml` from the old laptop to the same place on the new one, and fix any folder paths that differ. It names your repos, their clients, your `instructions` file, your private names (`[privacy] terms`) and your reply-check parts. Then clone each repo it lists into its `path` (or add `,url=...` to a `--repo` and let setup clone it), and run `nh setup` once. **Works when** setup lists your repos and `nh doctor` shows each of them.

No old settings to copy? Run setup with `--owner` and one `--repo` per repo, as in the install guide.

Your own instructions file (the one `instructions` names) and, if you keep one, your WWID profile repo (`wwid_profile`) come along too: copy or clone them to the paths the settings give, before running setup.

## 3. Restore each repo's secrets (yours to do)

Secrets never live in a repo or in nearly-headless. For each repo:

1. Open its README's setup section: it names the `.env` it needs.
2. Restore that `.env` from your password manager into the repo's main folder. One `.env` there serves all its worktrees.
3. If the repo's AGENTS.md says it uses the NetSuite toolkit, follow the `.env` part of [its setup guide](../toolkits/netsuite/README.md), and check it with `python3 <kit>/toolkits/netsuite/query.py --env sb --check` from the repo's folder.

If you bill with the [invoicing toolkit](../toolkits/invoicing/README.md), restore your billing profile to the path its README gives.

## 4. Check, then restart your supervisors

`nh doctor` should be all `ok` (see the install guide's table for each `FIX`). Then `nh serve`, and for each list on the command center, open a new chat in the Code tab on that repo and say "Resume as supervisor of `<product>`". Chats themselves stay on the old laptop; the lists live in their repos, so nothing is lost. Unfinished pieces restart from their PR and note.
