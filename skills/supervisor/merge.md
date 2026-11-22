# Merge, release, tear down

## Before the yes: review by what the change does, two rounds at most
Review by what a change does, not its file type. Anything that changes what agents or code do (skills, hooks, code, tests, settings, workflows) is reviewed, each round by a fresh read-only subagent (not a fork; it posts nothing):
1. Round 1: a fresh **Opus** reviewer (`model: "opus"`), given only the PR link, sha and repo, reviews the full PR (reading it; tests as `brief.md` says) and reports blockers (breaks behaviour, loses or exposes data, skips the owner's yes, contradicts the PR's goal) and at most 3 should-fix; no nits. Message the builder the blockers and cheap should-fixes.
2. Round 2: a fresh **Sonnet** reviewer (`model: "sonnet"`), given only the fix diff (`git diff <round-1 sha> <new sha>`) and the round-1 findings, checks it resolves them. With nothing to fix after round 1, round 2 reviews the full PR. Never resume the round-1 reviewer.
3. Never more than two rounds: after round 2, check any last fix yourself; anything left is a risk in the approval request.

Only a pure typo or wording fix that changes no behaviour skips review, and the skip is posted on the PR in one line (`Review skipped: wording only, no behaviour change`). A skill's wording is behaviour: it changes what agents do. Shas that only merge main or bump the version need no new review. Name each reviewer's model on the PR with its findings and their handling.

A reviewer of a NetSuite PR bound for production is also briefed with the kit's `toolkits/netsuite/audit.md`.

Before asking, clear the PR's `nh check` finding "Needs the latest main" with `nh refresh --pr <N>`, from the PR's repo: it merges main in the builder's worktree; clean, it pushes, waits for CI, posts `Ready @ <sha>` and prints that sha. It refuses, changing nothing, where CI isn't the full proof (the latest Ready doesn't say `Gap checks: none`, or the PR touches the map's full-run row), on a conflict (aborted) or failing CI (no Ready): then message the builder to refresh (take main, re-run the mapped checks, re-screenshot, report the new sha). A batch of releases goes in order, each refreshed after the one before merges.

## What a yes covers
- A yes covers only the PRs and the scope it names, at the sha you brought it.
- A yes given before a PR exists covers building it. The merge still needs the exact sha brought back, unless the owner's words say to merge as soon as review is clean; then post that carried-over yes on the PR, quoting the owner's words, before `nh merge`.
- "Deploy" or "release" names production separately: a merge yes is never a production yes, and "release when confident" covers only what it names.

## Merging, on the owner's yes given to you
From the PR's repo, run `nh merge --pr <N> --sha <approved sha>` (add `--piece P<n>` to set the piece merged, `--release vX.Y.Z` where the repo releases by tag; `--dry-run` prints the steps). Never run `gh pr merge` yourself: the merge-guard hook refuses it in a supervisor chat. It stops at the first failure with a plain reason:
1. Posts `Approved by the owner: merge @ <sha>` (no person's name; no story in any comment on a public repo).
2. Runs `nh gate fresh` at that sha and prints its verdict: main changed none of the PR's files since its branch last took main (`.work/` never counts), the four sections, and private names on public repos. It carries the yes over to a new head only after clean merges of main with CI green, and posts that. If it refuses on main, refresh as above, then run `nh merge` again; ask the owner again only when it says a new yes is needed.
3. Requires every CI check on that head to have completed green (skipped or cancelled is not green).
4. `gh pr merge --merge --match-head-commit <head>` (never squash or rebase), then confirms the merge commit holds exactly the approved files.
5. With `--piece` and `--release`, sets the piece merged, then tags and releases (below).
6. Tears down last: the PR branch's worktree, `git branch -d` after an ancestor check (never `-D`), the remote branch, confirmed with `ls-remote`. A branch checked out in the main checkout or in the worktree you run from keeps its local part: a `LEFT` line gives the commands, and the run still succeeds.

If a run stops after the merge, fix what it names and run the same `nh merge` again: it finds the PR merged at the approved code and finishes the remaining steps.

Where green CI isn't the proof (no CI, CI that can't run, or a PR that changes the CI itself: `.github/workflows/`, or anything the CI runs to decide what it tests), the **full run** must pass on that exact version, in the builder's worktree or a fresh one at that commit, and the owner's yes must name it. Never switch the repo's main checkout: other supervisors record their work there.

A stacked PR GitHub calls CONFLICTING while git merges it cleanly: merge main into the PR, check its code is identical to the approved sha (`git diff --quiet <sha> <new head> -- . ":!.work"`), post that on the PR, and once CI is green on the new head, `nh merge` it without a new yes.

## Releasing
Production gets only what is merged, from main, with its own yes; `nh merge` never deploys. A repo that releases by tag (this kit) releases with `nh merge ... --release vX.Y.Z`: it checks the version file says X.Y.Z at the sha, tags the merge commit by its id (never HEAD), pushes the tag and runs the repo's release steps (for the kit: `nh update`, then waits up to 2 minutes for the command center to show the new version). Any other repo needs `release = [...]` in its settings first (`[]` tags only), since a pushed tag can trigger a deploy.

## Tearing down a piece that ends without a merge
**A piece that ends `done` with a builder** (its outcome is in the note): `git fetch origin`, then check each of the builder's branches, whatever its name (including `worktree-agent-<id>`), has no commits outside the base branch (`git rev-list --count origin/<base>..<branch>` prints 0). If any has, keep its PR and branches and ask the owner before deleting them, as a numbered Needs you item. If none has, close its PR if any, then `git worktree remove <worktree>`, `git branch -d <branch>` (and the worktree's own base branch, also `-d`), and `git push origin --delete <branch>` where a remote copy exists; confirm `git ls-remote --heads origin <branch>` prints nothing.
