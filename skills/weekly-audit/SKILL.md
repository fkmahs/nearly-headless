---
name: weekly-audit
description: Run the weekly improvement audit of this laptop's Claude Code chats against the kit and the owner's instructions - counts by `nh audit measure`, judging by a few small read-only agents, one private report, and the top improvements brought to the owner as one numbered list for a yes. Use when the owner tells the supervisor of the kit's own list to run the weekly audit.
---

# Weekly audit

You are the supervisor of the kit's own list. Measuring is code, judging is small agents, you keep only conclusions. Everything this audit writes (counts, findings, quotes, the report) stays in the private audits folder `nh audit measure` prints: never in a repo, a PR, a GitHub comment, or a list note beyond each improvement's plain name.

1. **Measure.** Run `nh audit measure` (the last 7 days by default). It prints its folder (`audits/<date>/` beside the laptop's settings), holding `measure.json` (per chat: project folder, role, owner messages, turns, own tool calls per turn, agents, browser calls and browser picking, shell calls, compactions, merges and how many came after a fresh gate, push notifications, keyword counts) and `measure.md`. Read last week's `report.md` in the newest earlier `audits/` folder, if there is one.
2. **Split.** Group the chats with activity by project folder, about 300 MB of transcript per group: a few groups, not one per chat. Start one fresh small read-only agent per group, all at once, each given `rubric.md` from this skill's folder, its chats' transcript paths and rows from `measure.json`, and the file to write its findings to (`findings-<n>.md` in the audits folder). Each returns only a short summary.
3. **Combine** into `report.md` in the same folder:
   - What went well.
   - Patterns across chats, each with its numbers from `measure.json`.
   - Improvements to the kit, ranked, each with the pattern it fixes and its evidence (checked or inferred).
   - Problems outside the kit: one line each, naming the supervisor that should get it.
   - Since last week: for each improvement accepted last week, whether its numbers moved.
4. **Bring it to the owner:** the report's headline, then the improvements as one numbered list for a yes. "Proceed" means all of them.
5. **Record:** each accepted improvement becomes a piece on the kit's list (`nh piece add`, its note the pattern and evidence in generic words). On this audit's own piece, note which were accepted and which declined, so next week's run checks them.
