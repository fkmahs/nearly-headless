# Keeping work moving

Agents stop for the owner only when they truly need them. Work piles up while the owner sleeps or is away, so every question they didn't need to answer is hours lost.

## Go ahead, or ask
A step that is **reversible and inside the piece's agreed scope** goes ahead: do it, and report it in your next reply under **Done:** or **Started:** (or Still running), never as a question. For example:
- starting a builder in sandbox before a client's data arrives;
- starting a queued piece that `nh piece next` marks **start now**, with its brief;
- fixing something in the PR before release prep;
- an investigation whose recommendation is "work it out from the data": work it out;
- a design choice that can be undone before merge;
- a rules or skill change that reaches the owner as a PR merge anyway.

Always the owner's, however small: merges, deploys and live writes, deletions, credentials, hours, messages to anyone else, a change of scope, starting a piece that isn't in the queue they approved, and business choices that change how the client's staff work. Unsure whether a step can be undone, or is in scope? Then it is the owner's.

**On every start or resume**, after `nh check`, review your list's waiting questions (`nh question list <repo> <product>`). One you can now decide under this rule: withdraw it with `nh question withdraw <repo> <product> Q2 --reason "<what you decided and why it can be undone>"` (it leaves the list and the reason goes in its piece's note, marked as yours, never as the owner's answer; a whole-list question adds `--piece P3`, the piece whose note keeps it), act on it, and tell the owner in one line each under Done: or Started:.

## Asking the owner
**A new decision** gets a card below the block: the decision in one line; 2-3 options, recommended first and marked, each with what happens and how to undo it; what you do meanwhile. Use the app's end-of-turn buttons when available. Always record it, including one a builder stopped for: `nh question add <repo> <product> "<decision>" --options "A (recommended): <what happens>; undo: <how> | B: ..." --recommended "A" --pr N --piece P3`. Every question names its piece (a question about X's scope goes on piece X); only one about the whole list takes `--list-wide` instead. Tie an older open question with `nh question set <repo> <product> Q2 --piece P3` (or `--list-wide`). Record the owner's answer with `nh question answer` and say it on the PR. **New approvals** come as one numbered list: the change, the proof, the risk.

## The approved queue
So that a merge waiting on the owner never stops the list, at a check-in the owner can approve an **ordered queue** of next pieces (the catch-up's order is a good start).
- Each queued piece is a to-do with its **brief in its note** (`brief.md`) and the **files it will touch**: `nh piece set <repo> <product> P5 --files "wk/work.py,skills/" --after P3` (paths, folders ending in `/` or globs; `--files none` for work that touches no repo files; `--after`: the pieces whose merge it needs first).
- Record the order they approved in one write: `nh piece queue <repo> <product> P5,P7,P2`, plus a note line on each ("<owner>: queued at the check-in"). `nh piece queue <repo> <product> none` empties it. Starting a piece takes it off the queue. The owner can reorder or drop any piece at any time: re-record it at once.
- **Whenever a builder hands back, a merge starts waiting, or you start or resume**, run `nh piece next <repo> <product>` and start, at once and in order, every piece it marks `start now`, each with its own brief (`brief.md`); report them under Started:. Nights count too: ask the app to keep the laptop awake while the queue has pieces.
- It allows `builders_per_list` builders at a time per list (set in the laptop's settings; no limit when unset): a piece being built counts, one waiting on a merge doesn't. It holds back a piece whose files overlap an unmerged PR (any list's), a turn or a piece being built, or whose `--after` pieces haven't merged, and says why. Never start a held piece by hand; the reason shows in recall and the command center.
- The queue only starts work. Merges, deploys and releases still wait for the owner's yes (`merge.md`).
