# Laying out the ready work

When the owner asks what can run (lay out the ready work, all the open work, what can run on its own): one pass over every list in the client's repo, so the owner releases work in one reply.

## Gather from the records, not memory
For the client's repo: `nh status` and `nh --json product list <repo>` (each list and its supervisor's chat), then for each list `nh piece list`, the note of each open piece (`nh piece note`), `nh --json question list` (each question with its options and recommendation) and `gh pr list`. Say how many pieces and lists you read.

## Risks first
Before the tables, what would stop a run: two items touching the same files (say they will run one after another), a shared test environment's sign-in or turn, anything that would reach production. Nothing in the run touches production.

## Four tables, numbered across all four
Numbering runs on from table to table, so the owner can reply "send 1, 3, 7".
1. **Ready to deploy:** # · Supervisor (linked to its chat) · Work item · PR (linked) · What the yes covers (merge, sandbox look, or production release). One row per PR or release waiting on the owner's yes, from `gh pr list` and the merge and release questions in `nh --json question list`.
2. **Ready to start:** # · Supervisor (linked to its chat) · Work item (its plain name) · Why it's ready (one line: approved when, what it touches, sandbox, read-only or plan only).
3. **Ready once you answer:** # · Supervisor · Work item · The question itself, in full, with its recommendation. An answer releases the item into this run.
4. **Not ready yet:** one line per reason (waiting on a named role, a go-live), not per item.

## Inline replies
The owner may quote any row and reply under it. Read each reply as a decision on that row only. Unreplied rows in Ready to start go only if the owner said "all" or "the rest". A reply on a Ready to deploy row is the owner's yes for exactly that PR at that sha: pass it to that list's supervisor, who merges or releases. You never merge yourself.

## Hand-off
One message per supervisor: its items, the owner's words quoted exactly, the scope limits (sandbox only, plan only, no production), the order where files overlap, and the risks. Record each answered question with `nh question answer` and a dated line in each piece's note. You still never start builders on other lists: their supervisors do, and reply with what they started.

## Close
What went to whom, what was skipped and why, and what still waits on the owner.

## Checking what came back
When the owner asks what came back: one table, # · Supervisor · Work item · Result (PR ready for review, linked; stopped and why; not started), from each list's records and PRs, then the Ready to deploy table.
