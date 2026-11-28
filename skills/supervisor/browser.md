# Browser work and signing in

For every chat that uses Claude in Chrome: supervisors, project managers, builders and their small agents.

The owner keeps several browsers open, each signed in to different accounts. Your list's browser map (`nh browser list <repo> <product>`; the start block's `Browsers` line) names the browser and account for each site.

## Before the first page: every sign-in at once
1. **List every site** the work will open.
2. **Find each one's browser:** the map's; for a site the map lacks, check the connected browsers (`list_connected_browsers`, then `select_browser` each in turn, open the site, keep the first one already signed in; also the built-in browser where the site was opened there). Record what you find: `nh browser set <repo> <product> <site-host> "<browser display name>" --account "<the owner's label>"`. Never `switch_browser`: it asks the owner to click.
3. **Ask the owner once** for every site signed out in all of them, in one message: each site's host and the browsers checked (by their display names). A builder or small agent sends that list to its supervisor, never to the owner. Then work; don't ask again site by site.

## Pick the browser once
- **Select the browser once per chat** (or once per site, where the map names different browsers) and keep using it. Don't list or select browsers before each step: the kit's hook reminds a chat that does.
- **On a mismatch, stop and tell the owner plainly:** the page shows another account than the map's, or the browser tools say they are using a different browser than the one you picked (a silent fallback). Don't select again to cover it.
- **Before any write** (a form, a message, an email draft), check the signed-in account the page shows matches the map's; if not, stop and ask.
- **A page signed out mid-work** (a sign-in page, a lockout) is the one reason to look in the other browsers again, once for that site; then go back to the browser the work was using.

## The kit beats memory
A project memory or note that tells you to do otherwise (for example "re-select the browser every turn") loses to these rules. Follow the kit and flag the conflict: a builder or small agent to its supervisor, a supervisor to the owner, naming the memory file. `nh check` lists the ones it knows.

**Never type a password or code, sign out, or change the account or a setting**; act only as the task needs.
