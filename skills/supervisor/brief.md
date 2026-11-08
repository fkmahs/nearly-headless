# Starting a builder

Start it when work on the piece starts, not before: the Agent tool, in the background, with `isolation: "worktree"`, told to invoke the `builder` skill and follow it. Then `wk piece set <repo> <product> <id> --chat agent:<the name the Agent tool gives it>`.

The brief, sized to the piece:
- the product's **standing orders, verbatim** (`wk order list`);
- **goal** and why, in the owner's words; for a public repo (`public = true` in the settings), only the generic what and why: no client, employer, people or piece numbers, since the builder's public text follows the brief. The private reason stays in the list note;
- **scope**;
- **acceptance**: what done looks like;
- **how to verify**;
- **forbidden**;
- **what to report**: the PR, its head sha and CI's result on it, and the map's checks it ran locally with their result (tests as below);
- to load every deferred tool it needs in one `ToolSearch` call (`select:A,B,C`), naming them where you know them;
- a **scratch folder of its own**, named after its piece (builders that share one overwrite each other's files);
- for an investigation, a **stop point** sized to the question (e.g. "after 30 tool calls, or once the cause is found"): there it returns a partial finding with what's left.

## Not builder work
Any agent starts at tens of thousands of tokens, and a builder adds a worktree, a PR and tests. Only a code change that ends in a PR gets one. Everything else, cheapest first:
- **Yourself:** a lookup of a few commands, a data check, a question a few read-only queries answer. Where a repo's AGENTS.md names a read-only query tool (a client repo's API query tool, for example), query first; the browser is only for what users see.
- **A fresh small agent**, short query-first brief, no worktree, no sandbox tests, no browser except for an approved write itself: a longer data check, an investigation, a data fix the owner already approved. Its written finding goes in the piece's note.
- **A scope change mid-run:** stop the agent and start a fresh one with the new short brief. Never resume it with a re-brief: a resumed agent re-reads its whole old context on every call.
- **A small follow-up** (a check, a question, a tweak outside the PR's fixes): a fresh agent with a short brief, never the finished one resumed. Message a builder only to continue its own PR: review fixes, or a refresh `wk refresh` can't do. A builder that can't be woken: start a fresh one from the PR and the note.

## Tests in a brief, a review or a message
A local full run takes most of an hour where CI takes minutes, so never ask for one:
- a brief asks for the map's checks for what the builder changed (`wk test` runs them), then CI on the ready PR as the full proof. Never ask for test counts "before, after, new" or a baseline run: they push a full run;
- a reviewer runs no tests: CI's result on the sha and the builder's `Ready @ <sha>` comment are the proof. Told to check a fix, it may run that fix's mapped checks;
- a refresh or review-fix message says "re-run the mapped checks", never "run all the tests";
- the only local full run is the map's full-run row (a change to what CI runs) or CI that can't run (`merge.md`).

## Reading what comes back
- Its claims are marked **checked** (with how) or **inferred** (from what). Relay an inferred claim as inferred, or check it first with one command.
- While it runs, its PR's latest `Progress:` comment is where it stands (`gh pr view <N> --comments`).
