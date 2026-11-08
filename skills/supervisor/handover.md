# Handing work over

## Taking over an existing chat
It can't be nested under you, so it hands off:
1. It pushes its work and writes a handoff note in its PR, or in its reply to you when it has no PR.
2. You put that note in the piece's note (`wk piece note ... --file`), then start a builder from it when work resumes.
3. Archive the old chat once nothing is unpushed.

## Handing a repo over to a client
Remove `.work/` first. It holds the owner's work state and hours, never the client's.
