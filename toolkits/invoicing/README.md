# Invoicing toolkit

Optional. Bills a client by the hours its repos record in `.work/hours.tsv` through `wk`. It makes the invoice, and has `wk hours mark` label those hours with the invoice number so nothing is billed twice. It also makes sure no finished work is missed: hours name the pieces they pay for, and an invoice is refused while a piece finished since `coverage_from` is neither paid for nor marked bill later or not billing.

**A client's billing record lives in its own repo**, in `billing/` (never in `.work/`, never in work-kit):
- `client.toml`: invoice prefix, the repos whose hours it bills, rate, terms, bill-to, each list's group and ref, the calls list (`calls_product`), and `coverage_from = "YYYY-MM-DD"` (quoted): the first day whose finished work must be billed (for a client moving from another tool, the day after its last invoice there).
- `ledger.json`: every invoice made.
- `<ID>.md`: each invoice's frozen statement, one readable file: the invoice lines as printed, then its **Hours** (one row per entry) and **Lists** (each list's ref, group and invoice line, one row per named line) tables, with a closing **Line key** column only when some hours name a line, which the toolkit reads back exactly to print the PDF and run `check`. `make` refuses to write one that wouldn't read back exactly. The ledger keeps its hash.
- Invoices made before 1.30 also keep `<ID>.csv`, and stay read from it: nothing already made is rewritten or re-hashed, and `wk check` doesn't flag those files. New invoices write no CSV.

Remove `billing/` with `.work/` before handing a repo to a client.

Needs the owner's billing profile at `%USERPROFILE%/.wk/billing-profile.json` (outside `C:/Projects`). A repo uses this toolkit when its AGENTS.md says so.

## Friday

The client's project manager runs it with the owner, in one pass: see the `project-manager` skill in work-kit's `skills/`. In short: the week's finished work per list, the hours booked with the pieces they pay for, the owner's bill later or not billing choices, then `preview`, his yes, and `make`.

`py <work-kit>/toolkits/invoicing/invoice.py` with `<repo>`, the client repo holding `billing/`:
- `preview <repo> --through <Friday>`: what the owner reads before his yes, as Markdown tables built from the records (hours and the pieces they name, bill marks, piece states and PRs), shown even when no hours are booked yet:
  - **Billed:** Area (list) | Exact line on the invoice | Pieces it pays for | Hours; then the calls, one row each; then the total hours and amount; then, when there are any, pieces billed before that have new hours (Area | Piece | New hours).
  - **Carried forward:** finished, billing later (Area | Piece | Reason); in progress with work in it, meaning building, ready, or with hours booked (Area | Piece | Where it stands: state, PR, and whether part of it is billed on this invoice); not started (Area | Piece), grouped by list.
  - **Not billing:** Area | Piece | Reason, for pieces still open or finished in this invoice's period (a first invoice's from `coverage_from`).
  - **Fix before make:** finished pieces not covered and every other problem; these block `make`.
  - `--json` prints the data instead.
- `make <repo> --through <Friday>`: refused while preview lists problems. Freezes the statement, records it in the ledger, marks the hours in each repo and prints the PDF to `Documents/Invoices`.
- `pdf <repo> <ID>` prints an invoice's PDF again; it refuses when the statement no longer matches the hash in the ledger, so a hand-edited statement never prints.
- `sent <repo> <ID>`, later `paid`, once the owner has sent it.
- `check <repo>` proves every statement still equals the hours marked with its number, nothing changed after it was made, and shows billed and outstanding.

A piece counts as finished on the day it last became merged, or `done` for work finished without a PR (a finding, an outcome), after being open, or became merged after done; a piece merged before work-kit 1.21 kept that date counts as finished on the day its row last changed, which is never before it merged. It is covered by unbilled hours, or by billed hours dated, or on an invoice whose period ends, on or after that day: hours billed before it finished paid only for the work up to then. The calls list holds no build work, so its pieces are not checked.

A work-kit older than 1.22 doesn't see `done` pieces, so the invoicing laptop runs `wk update` before invoicing.

**Before anyone uses `--for` or `--bill`, run `wk update` on every laptop that records work in the client's repos.** An older work-kit rewrites the hours and pieces files without those columns, which loses the piece links.

## Several rows for one list

A list prints one invoice row: its line (`wk product set <repo> <list> --line "..."`) and its unbilled hours. To bill some of its hours as a row of their own, give the list a named line and book those hours on it:
- `wk product set <repo> <list> --line fixes="Fixed errors in the reports"` adds or rewords the line `fixes`; `--line fixes=` removes it, refused while unbilled hours bill on it.
- `wk hours add ... --line-key fixes` books hours on it; `wk hours fix <repo> <list> --date <day> [--was "..."] --line-key fixes` moves hours already booked (unbilled) onto it, and `--line-key ""` back to the list's usual line.

The invoice then shows one row per line: the usual line first (hours with no key, exactly as before), then each named line, all under the list's ref and group. The preview's **Billed** table shows the same rows.

## Rules

- Only hours the owner stated are billed. Never estimate.
- Every finished piece is billed, billed later or not billed, by the owner's choice, with the reason in its note. Hours for work billed later are booked when it is billed.
- An unbilled entry is corrected with `wk hours fix`; a billed one never changes.
- Invoice lines are for the client's accountants: what was built or done and the business problem it solves. No ticket IDs, file names or jargon. Custom development is named in bold.
- A made invoice is never edited. Numbers never skip or repeat.
- No bank details, tax IDs or client PDFs in any repo.
