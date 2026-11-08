---
name: catch-up
description: Brief the owner on a supervisor's list of work - one table of every open piece, in the order the supervisor recommends doing them, then what it needs from the owner. Use whenever the owner asks a supervisor to catch them up, asks where things stand, or what's next.
---

# Catch up

First reread the list from the repo, never from this chat: `wk piece list <repo> <product>`, `wk piece note <repo> <product> <id>` for each open piece, `wk question list <repo> <product>`, `gh pr list`; then run `wk check <repo> <product>` and fix what it lists, so the table is true.

Answer with one table of **every open piece**, in the order you recommend doing them:

| # | Piece | What it gets the owner | Where it stands | Next, and who has it |
|---|---|---|---|---|

- **What it gets the owner:** one plain line a product manager reads at a glance; no code, file or ticket names.
- **Where it stands:** not started, builder working, PR #N open (draft or ready), waiting on the owner, or parked; plus the work done so far, in a few words.
- **Order:** first what unblocks or de-risks the others, then what the owner is waiting on, then the rest. Under the table, one line for each place that needs a reason ("1 first: 3 and 4 build on it"). Then record it for the command center: `wk piece rank <repo> <product> P3,P1,P7` (every open piece, first to last).
- End with what you need from the owner as one numbered list, or "nothing".
