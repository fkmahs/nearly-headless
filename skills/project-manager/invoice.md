# The Friday invoice

One pass with the owner, not one per list. `<repo>` is the client repo holding `billing/`; `invoice.py` is `toolkits/invoicing/invoice.py` in nearly-headless. Run `nh update` first, every time: an older nearly-headless skips `done` pieces.
1. **Finished work, per list:** run the preview (step 5) first and show its tables: what is carried forward, what is not billing, and under **Fix before make**, finished work not billed yet.
2. **Hours:** the owner gives the week's hours per list in one go. Book them on each list, naming the pieces they pay for: `nh hours add <repo> <product> <h> --date <day> --note "..." --for P3,P7`. Never estimate. A wrong unbilled entry: `nh hours fix`.
3. **His choices:** work he won't bill now: `nh piece set <repo> <product> <id> --bill later --reason "..."`; its hours are booked on the Friday he bills it, after `--bill yes` (hours for a piece marked later or no are refused). Work he won't bill at all: `--bill no --reason "..."`.
4. **Invoice lines:** each list's line describes the week's work (`nh product set <repo> <product> --line "..."`), written for the client's accountants. When the owner wants some of a list's hours on a row of their own: `--line fixes="..."` adds that line, and `nh hours add ... --line-key fixes` (or `nh hours fix ... --date <day> --line-key fixes` for hours already booked) puts hours on it.
5. **Preview:** `py invoice.py preview <repo> --through <Friday>`. Show him its output as it is: tables built from the records, never by hand.
   - **Billed:** one row per invoice line, so a list with named lines has several (Area (list) | Exact line on the invoice | Pieces it pays for | Hours), then the calls, one row each, then the total hours and amount, then any pieces billed before that have new hours. The exact line is what prints, so he approves the wording here and needn't read the PDF in detail.
   - **Carried forward:** finished, billing later (with the reason); in progress with work in it (where it stands: state, PR); not started, grouped by list.
   - **Not billing:** each piece with its reason.
   - **Fix before make:** every problem; `make` refuses while any is left, including a finished piece not covered.
6. **On his yes:** `py invoice.py make <repo> --through <Friday>`. He reviews the PDF and sends it himself. Then `py invoice.py sent <repo> <ID>`, later `paid`.
