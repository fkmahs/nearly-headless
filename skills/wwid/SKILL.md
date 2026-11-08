---
name: wwid
description: What Would I Do, the WWID skill. Reads the WWID profile repo (wwid_profile in the laptop's settings; off without one). Suggest mode (the current pilot) - at the end of every supervisor reply to the owner, add a short "What would I do" section predicting how they'll answer, then record whether they agreed. Responder mode (v2, auto-replies to waiting chats) is off until the owner starts it themselves.
---

# What Would I Do (v3: suggest-mode pilot)

**Suggest mode** (on): every supervisor reply to the owner ends with a guess at how they'll answer; their verdicts are kept, so the profile improves daily. **Responder mode** (v2) answers waiting chats for them; it is off until the owner starts it themselves: only then read `responder.md` in this folder.

**The owner** is the person `owner` (or `approver`) names in this laptop's settings file (`~/.workkit/settings.toml`, or the file `$WORKKIT_SETTINGS` names); call them by that name.

**First, find the profile repo.** The evidence lives in the WWID profile repo, outside work-kit: the folder `wwid_profile` names in that settings file (`wk doctor` shows it). It is written `wwid/` below: how they decide (`wwid/patterns.md`), profiles, decision episodes, replays, research, and `wwid/verdicts.md`.

**Off without it.** When `wwid_profile` isn't set, or its folder has no `verdicts.md`, WWID is off on this laptop: add no section, record nothing, and if asked, say in one line that WWID is off because no profile repo is set (`wk setup --profile <folder>` turns it on).

## The section
At the end of **every reply a supervisor sends the owner**; never in builder reports or worker chats.
1. Finish your reply, then read it as the owner will, caught up: the piece's note and decisions, and what they said last in this chat.
2. Predict their answer with `wwid/patterns.md`, `wwid/data/triangulation.md`, and the disagreements in `wwid/verdicts.md` after its last `<!-- refined through ... -->` line (they show where this profile is wrong).
3. Write it, under about 60 words, to them, under this exact heading (the reply check looks for it):

   > **What would I do**
   > - *(their most likely answer to what the reply asks, e.g. "yes, merge it" or "not yet")*
   > - *(1 to 3 more: what they'd most likely push on or ask next: a missing proof, a scope worry, the next piece)*
   >
   > Agree? Reply **agree**, or tell me what you'd actually do.

- **Honest, not flattering:** predict where they'd push back on your own reply.
- **A prediction is never an answer.** A predicted yes on a merge, deploy, deletion, credential, hours or a message to someone else is not a yes.
- No client names (write "a client"), money figures, account numbers or client personal details.
- **When unsure,** pick the reversible option that keeps the work moving, and say which way they might go instead.

## The verdict
The owner's next message is the verdict: **agree** (they say so, or answer as the first bullet did), **disagree** (they say so, or answer differently: their words are the lesson), or none (they move on: record nothing). Do what their message asks as usual, then record an agree or disagree as `record.md` in this folder says. When they ask to refine WWID, read `record.md` too.
