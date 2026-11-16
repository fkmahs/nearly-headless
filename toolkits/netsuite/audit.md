# NetSuite production audit

Run this before any NetSuite change goes to production: scripts, Suitelet pages, workflows, custom records, deployments. It sets the bar and the checks never to miss. How you test is yours: bring your own expertise and add any check this file doesn't name.

## The bar
- **Evidence** follows the builder skill's rules (prove it; passed, failed and unknown kept apart; checked or inferred; sandbox proof). Every finding cites file:line, a test, a query or a sandbox run.
- **Judge by the person using it:** someone in finance, AP or treasury, often in a hurry, and a product owner reading the PR.
- **Production is real money.** In doubt, assume the worst case: the double payment, the half-finished run, the wrong account.
- **Client specifics** (the repo's UI rules, framework and architecture doc) are wherever its AGENTS.md names them.

## Lenses (apply all; weight them by what the change does)

### 1. Limits and volume
- For every entry point, name the governance limit that ends it and the size where it breaks: usage units per execution (Suitelet and user event 1,000; RESTlet 5,000; scheduled 10,000; Map/Reduce per invocation: map 1,000, reduce 5,000, input and summarize 10,000) and time per execution (Suitelet, user event, RESTlet 300 s; scheduled 3,600 s).
- Result caps: `ResultSet.each()` stops at 4,000 rows; SuiteQL returns at most 100,000 rows without SuiteAnalytics Connect; large IN-lists, payload, URL and file sizes.
- Real volume: read production through this toolkit's `query.py` (read-only) for what real runs look like, then test the sandbox well above it, with the usage and batch limits production gives (never raised for the test).
- **The volume answer:** the largest safe size today, the limit that sets it, and what the user sees beyond it.

### 2. Failure and recovery
- Every step that writes: if it stops halfway, the record ends in a clear, named state.
- **Retry across steps:** before repeating any outward action (bank file, payment, email, integration call), check whether that step already ran; a rerun after a partial run never repeats it.
- **The final status tells the truth:** a run where everything failed never ends as done; a partial run ends in a named state; a stuck or queued run is closed or flagged after a time limit, never left open.
- **Concurrency:** a double click, two users, two instances, a deployment already queued.
- **Errors** are caught with context (record, user, step), logged, and shown in plain words with what to do next. No silent catch.

### 3. Data integrity
- The server re-checks everything the page sent: amounts, accounts, statuses, permissions. The page is never trusted.
- No dirty data: required fields, bounds, allowed values, and a stale-page check (has the record changed since it loaded?).
- Stored text (run summaries, logs on records) fits its field: read long values back and check nothing is cut off.
- Records only scripts should change are locked against hand edits.
- No duplicates or conflicts with native features and installed bundles (native bill payment, bank payment bundles).

### 4. Dates, currency and rounding
- Time zones: the user's, the company's and the server's; dates near midnight and month end.
- Multi-currency and subsidiaries: the right currency, exchange rate and subsidiary on every record.
- Rounding on split or allocated amounts: the parts add up to the whole, to the cent.

### 5. Controls (finance and banking)
- Every flow runs at least once as the real user's role, not an administrator. Roles and permissions, segregation of duties (prepare, approve, release), and an audit trail of who did what, when.
- **Money paths:** right account, entity, amount and date; anything already paid elsewhere is caught before it pays again.
- **Reversibility:** how a mistake is undone, and who can do it.

### 6. Security and logs
- No SuiteQL or search built by string concatenation of user input: use parameters.
- Record data put into Suitelet HTML is escaped.
- The execute-as role is no wider than the change needs.
- No secrets in code, deployments or logs; no personal or bank data in logs.
- Log level fits production (no debug flood), and log volume per run is bounded.

### 7. Architecture and fit
- Reuse the repo's framework before adding records, scripts or fields; say what was reused and what was created, and why.
- Other vendors' scripts are worked with as they are, never bypassed or modified.
- **No hard-coded internal IDs** that differ between accounts: list each one and check it exists in production (read-only).
- The repo's architecture doc and other docs are updated in the same change and say what is actually live.

### 8. The screen
- Read at a glance: short labels, chips and tiles, not sentences.
- **At 100+ rows** it still reads: rows collapse, lists cap with "+N more", the result links to a drill-down.
- **Every action:** a confirm that says what will happen, a busy state with buttons off, a result with the outcome and one next link.
- Follows the repo's UI rules; checked at the user's real window width.

### 9. Sandbox and release
- Sandbox differs from production: features, bundles, roles, numbering, and data. Name what differs and what it could hide.
- Sandbox runs have no live side effects: emails, bank files, integrations are off or pointed at test targets.
- Deployment settings: status, audience, log level, concurrency, execute-as role.
- Production setup: list every setup record and setting it needs, check each exists in production (read-only), and who creates any missing one; never copy sandbox values.
- Release objects and production deploys go through this toolkit's `sdf.py` (production compare, approval gates); don't restate them.
- **Rollback plan:** how to undo the deploy, and what to do with data the new version already wrote.

## Output (always this shape, in the PR)
1. **Verdict:** Ready / Ready with fixes / Not ready.
2. **Findings:** a table: lens · finding · evidence · severity (Blocker / Should fix / Note) · one-line fix.
3. **Safe volume:** the largest size that works today, and the limit that sets it.
4. **Still to prove:** each check not yet run, and how to run it.
5. **Your own checks:** what you tested beyond this list, and why.

Short, plain words. No finding without evidence.
