# Recording a verdict, and refining

`<profile>` is the WWID profile repo: the folder `wwid_profile` names in this laptop's settings file (see `SKILL.md`). Without one, record nothing.

## Recording
Append one line to `<profile>/verdicts.md`:

```text
- 2026-10-30 | nearly-headless/nearly-headless | PR ready for merge | predicted: yes, merge and release | disagree: "not yet, show me it running first"
```

Date | repo/list | what the reply was about | the first bullet | **agree**, or **disagree:** their words, shortened.

**Verdicts are written without client names**, so the profile can travel: for a client's repo write `client/<list>` (never the repo's or client's name), and in the other columns "a client" or a neutral label ("client A") instead of any client, company or person's name. Also leave out money figures, account numbers, credentials and client personal details.

Save it on the profile repo's `main`, touching nothing else:
`git -C <profile> commit --only -m "wwid: verdict" -- verdicts.md`, then `git -C <profile> push origin main` (if rejected: `git -C <profile> pull --rebase --autostash origin main`, then push again). Never switch branches in that checkout: if it isn't on `main`, don't write; tell the owner in one line.

## Refining
When the owner asks (the goal is daily), follow "How to refine WWID" in `<profile>/README.md`: from the verdicts after the last `<!-- refined through YYYY-MM-DD -->` line, their recent chats, or what they tell you. A disagreement that repeats becomes a rule under "Patterns to weigh" in `<profile>/patterns.md` (or a profile update), citing the verdict dates; after a verdicts refinement, add a new `refined through` line. The owner says yes to every refinement; it is one commit to the profile repo, pushed as above, with no client names. Only a change to how this skill works is a PR to nearly-headless.
