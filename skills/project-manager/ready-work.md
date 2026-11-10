# Laying out the ready work

When the owner asks what can run (lay out the ready work, all the open work, what can run on its own): one pass over every list in the client's repo, so the owner releases work in one reply.

## Gather from the records, not memory
For the client's repo: `nh status` and `nh --json product list <repo>` (each list and its supervisor's chat), then for each list `nh piece list`, the note of each open piece (`nh piece note`), `nh --json question list` (each question with its options and recommendation) and `gh pr list`. Say how many pieces and lists you read.

## Risks first
Before the tables, what would stop a run: two items touching the same files (say they will run one after another), a shared test environment's sign-in or turn, anything that would reach production. Nothing in the run touches production.

## Three tables, numbered across all three
Numbering runs on from table to table, so the owner can reply "send 1, 3, 7".
1. **Ready now:** # · Supervisor (linked to its chat) · Work item (its plain name) · Why it's ready (one line: approved when, what it touches, sandbox, read-only or plan only).
2. **Ready once you answer:** # · Supervisor · Work item · The question itself, in full, with its recommendation. An answer releases the item into this run.
3. **Not ready yet:** one line per reason (waiting on a named role, a go-live, a merge or release yes), not per item.

## Inline replies
The owner may quote any row and reply under it. Read each reply as a decision on that row only. Unreplied rows in Ready now go only if the owner said "all" or "the rest".

## Hand-off
One message per supervisor: its items, the owner's words quoted exactly, the scope limits (sandbox only, plan only, no production), the order where files overlap, and the risks. Record each answered question with `nh question answer` and a dated line in each piece's note. You still never start builders on other lists: their supervisors do, and reply with what they started.

## Close
What went to whom, what was skipped and why, and what still waits on the owner.

## Checking what came back
When the owner asks what came back: one table, # · Supervisor · Work item · Result (PR ready for review, linked; stopped and why; not started), from each list's records and PRs, then the yeses now waiting.
