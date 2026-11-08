# Merge, release, tear down

## Before the yes: review, two rounds at most
Every reviewed PR gets both models' views, each from a fresh read-only subagent (not a fork; it posts nothing).
1. Round 1: a fresh **Opus** reviewer (`model: "opus"`), given only the PR link, sha and repo, reviews the full PR (reading it; tests as `brief.md` says) and reports blockers (breaks behaviour, loses or exposes data, skips the owner's yes, contradicts the PR's goal) and at most 3 should-fix; no nits. Message the builder the blockers and cheap should-fixes.
2. Round 2: a fresh **Sonnet** reviewer (`model: "sonnet"`), given only the fix diff (`git diff <round-1 sha> <new sha>`) and the round-1 findings, checks it resolves them. Never resume the round-1 reviewer (a resume re-reads its whole context); reading a small final fix yourself doesn't replace round 2. With nothing to fix after round 1, round 2 reviews the full PR. Then stop: anything left is a risk in the approval request.

The riskiest changes (data, money, credentials, production writes, hooks, CI) always get both rounds.

Before asking, clear the PR's `wk check` finding "Needs the latest main" with `wk refresh --pr <N>`, from the PR's repo: it merges main in the builder's worktree; clean, it pushes, waits for CI, posts `Ready @ <sha>` and prints that sha. It refuses, changing nothing, where CI isn't the full proof (the latest Ready doesn't say `Gap checks: none`, or the PR touches the map's full-run row), on a conflict (aborted) or failing CI (no Ready): then message the builder to refresh (take main, re-run the mapped checks, re-screenshot, report the new sha). A batch of releases goes in order, each refreshed after the one before merges.

No reviewer for wording-only changes (no code, tests, hooks, settings or workflows: read the diff), or for shas that only merge main or bump the version. Post the findings and their handling on the PR.

## Merging, on the owner's yes given to you
1. Post `Approved by <owner>: merge @ <sha>` on the PR. On a public repo: `the owner` for the name, and no story in any comment.
2. **Check the proof on that exact sha.** Green CI is a run that completed with success on that sha (`gh pr checks <N>`, the PR's head at that sha); skipped or cancelled is not green. A repo with workflow files has CI: if no run completed on that sha, its CI can't run; never call that "no CI". The builder's `Ready @ <sha>` comment must list the gap checks passing. Green CI is the full proof, with no local re-run.
   - With no CI, CI that can't run, or a PR that changes the CI itself (`.github/workflows/`, or anything the CI runs to decide what it tests; its own green CI doesn't count): the **full run** (the whole test suite, every check the repo's map names, lint, type check and build where it has them) must pass on that exact version, in the builder's worktree or a fresh one at that commit, and the owner's yes must name it. If it didn't, tell the owner and ask again.
   - Never switch the repo's main checkout: other supervisors record their work there.
3. **Merge only if** step 2's proof holds and `wk gate fresh --pr <N> --sha <approved sha>`, run from the PR's repo, passes and names that repo (main changed none of the PR's files since its branch last took main; `.work/` records never count). It also checks the four sections and, on public repos, private names. If it refuses on main, refresh as above (`wk refresh`, or the builder where that can't stand in); run the gate again. The yes carries over to a new head H when the gate says "carried over" (only clean merges of main since, CI green on H; plus the builder's re-run sandbox check and screenshots where the repo has one): post `Approved by <owner> @ <sha>, carried over to <H> (clean main merge, CI green)`. Ask the owner again only when the gate says a new yes is needed.
4. `gh pr merge <N> --merge --match-head-commit <the head the gate passed>`. No `--delete-branch`: the builder's worktree still has the branch, so it fails; tear-down deletes it.
   - A stacked PR GitHub calls CONFLICTING while git merges it cleanly: merge main into the PR, check its code is identical to the approved sha (`git diff --quiet <sha> <new head> -- . ":!.work"`), post that on the PR, and once step 2's proof holds on the new head, merge without a new yes.

## Releasing
Production gets only what is merged, from main, with its own yes; you run the release yourself, from main. Tag `vX.Y.Z` only after `gh pr view <N> --json state` shows MERGED, on that PR's merge commit (`gh pr view <N> --json mergeCommit`), never on HEAD or an empty value.

## Tearing down, after every merge, without asking
1. `git fetch origin`; check the branch is in main (`git merge-base --is-ancestor <sha> origin/main`) and nothing is unpushed (`git -C <worktree> status` clean, branch level with `origin/<branch>`).
2. `git worktree remove <worktree>`.
3. `git branch -d <branch>` while its remote copy still exists (`-d` checks against it), then the worktree's own base branch (e.g. `worktree-agent-<id>`) if there is one, also with `-d`, so an unmerged one is kept.
4. `git push origin --delete <branch>`; confirm `git ls-remote --heads origin <branch>` prints nothing.

**A piece that ends `done` with a builder** (its outcome is in the note): `git fetch origin`, then check each of the builder's branches, whatever its name (including `worktree-agent-<id>`), has no commits outside the base branch (`git rev-list --count origin/<base>..<branch>` prints 0). If any has, keep its PR and branches and ask the owner before deleting them, as a numbered Needs you item. If none has, close its PR if any and tear down as above, checking "level with `origin/<branch>`" and deleting the remote copy only where one exists.
