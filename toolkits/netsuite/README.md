# NetSuite toolkit

Optional. For repos that work on a NetSuite account. Each repo keeps its own NetSuite credentials in its git-ignored `.env`; nothing about an account lives in work-kit. A repo uses this toolkit when its AGENTS.md says so.

| | |
|---|---|
| `query.py` | read-only SuiteQL over NetSuite's REST API; pages through the whole result and writes it to a CSV or JSON file |
| `sdf.py` | the SDF deploy tool: imports, validation, sandbox deploys under turns, production deploys from `main` behind the approval gates and a production compare. Settings come from the repo's `sdf.toml` |

## Setup (once per account)

In NetSuite, as an administrator:

1. **Turn on the features.** Setup > Company > Enable Features > SuiteCloud: *REST Web Services* and *Token-Based Authentication*.
2. **A role for queries.** Setup > Users/Roles > Manage Roles > New (or reuse one), with:
   - Setup: *Log in using Access Tokens* (Full) and *REST Web Services* (Full);
   - Reports: *SuiteAnalytics Workbook* (Edit) — SuiteQL needs it;
   - View on every record type you will query (Lists, Transactions, Setup), e.g. *Accounts*, *Subsidiaries*, *Find Transaction*. A query can only see what the role can see.
   Give that role to the user the token will act as.
3. **An integration record.** Setup > Integration > Manage Integrations > New: *Token-Based Authentication* on, *TBA: Authorization Flow* and *Authorization Code Grant* off. Save, and copy the **consumer key and secret** (shown once).
4. **An access token.** Setup > Users/Roles > Access Tokens > New: that integration, user and role. Save, and copy the **token ID and secret** (shown once).
5. **The account ID**: Setup > Company > Company Information, e.g. `1234567` (production) or `1234567_SB1` (a sandbox).

Put the five values in the password manager, then in the repo's `.env` (git-ignored; one `.env` in the main checkout serves its worktrees). One block per account, named by the `--env` you will pass:

```
NETSUITE_SB_ACCOUNT_ID=
NETSUITE_SB_CONSUMER_KEY=
NETSUITE_SB_CONSUMER_SECRET=
NETSUITE_SB_TOKEN_ID=
NETSUITE_SB_TOKEN_SECRET=

NETSUITE_PROD_ACCOUNT_ID=
NETSUITE_PROD_CONSUMER_KEY=
NETSUITE_PROD_CONSUMER_SECRET=
NETSUITE_PROD_TOKEN_ID=
NETSUITE_PROD_TOKEN_SECRET=
```

A repo whose `.env` already uses other names passes `--prefix`, e.g. `--prefix ACME_SB_`. `NETSUITE_ENV_FILE` points at a different `.env`.

Check it (shows which keys are set, never their values, and runs one tiny query):

```
py <work-kit>/toolkits/netsuite/query.py --env sb --check
```

## Opt in

Add to the repo's AGENTS.md:

```
## NetSuite
Query NetSuite with work-kit's toolkit, from this repo's folder:
`py <work-kit>/toolkits/netsuite/query.py --env sb|prod "<SuiteQL>" --out <file>.csv|.json`
Credentials: NETSUITE_<ENV>_* in this repo's .env. Read-only; never print the values.
```

## Querying

```
py <work-kit>/toolkits/netsuite/query.py --env prod --file q.sql --out out/result.csv
```

- Run it from the repo's folder; it reads that repo's `.env`.
- `--out` writes CSV or JSON by its extension; without it, a table on screen. `--max-rows` stops early.
- Only one `SELECT`/`WITH` statement; anything else is refused.
- A result over several pages needs an `ORDER BY` on a unique key, or NetSuite may repeat or skip rows.
- NetSuite pages through at most 100,000 rows per query; split larger pulls with a `WHERE` on that key.
- When NetSuite is busy (HTTP 429) it waits and retries.

## SDF deploys (`sdf.py`)

One deploy tool for every NetSuite SDF repo. The repo keeps a thin `tools/sdf.py` that loads this kit by path and calls `main(argv, root=<repo>)`, plus an `sdf.toml` at its root. Everything account-specific lives in `sdf.toml`; the kit holds no account names or IDs.

```toml
requires = "1.39.0"                  # oldest work-kit this repo needs; older refuses until `wk update`
own_folder = "/SuiteScripts/ACME/"   # File Cabinet folder `upload` may write without the replace flag
wrapper_env = "ACME_SDF_WRAPPER"     # set to 1 on every suitecloud call (suitecloud.config.js checks it)
reference = "reference/vendor"       # where `--reference` imports go (never deployed)
replace_in_place_flag = "replace-vendor"   # flag that lets `upload` overwrite a file outside own_folder
tool_files = ["tools/sdf.py", "sdf.toml"]  # main having newer commits on these refuses deploy/upload
entry = "py tools/sdf.py"            # how messages tell people to run it

[sb]
auth_id = "acme-sb"                  # SuiteCloud auth ID
domain = "1234567-sb1.app.netsuite.com"
env_prefix = "ACME_SB_"              # <prefix>ACCOUNT_ID in .env must match the auth ID's account

[prod]
auth_id = "acme-prod"
domain = "1234567.app.netsuite.com"
env_prefix = "ACME_PROD_"

[compare]
ignore = []                          # object fields that never count as a difference
```

`requires` and each env's `auth_id` and `env_prefix` are required: without them every command refuses. Every call also refuses when `.env` names no `<env_prefix>ACCOUNT_ID` (the main checkout's `.env` serves its worktrees, wherever they live).

What it keeps for every repo:
- `sb` or `prod` is always explicit, and the auth ID's account must match `.env`.
- Production deploys and uploads run only released code: the work-kit whose `wk` runs the gates, and the checkout holding this kit if that is another one, must have no changes in `toolkits/netsuite` or `wk` and be on work-kit's `origin/main`.
- Deploy scope is picked by PR: a sandbox deploy sends what the branch changed since its merge-base with `origin/main`, under `wk turn` on those files; `release sb` puts main's version back and frees the turn.
- Production runs only on `main`, level with `origin/main`, after the merge, behind `wk gate release` and `wk gate approved`; it tags `prod-<date>-pr<N>` and checks release ancestry.
- `upload` sends files only (no objects); imports take `--reference`; a Java agent maps `|` in File Cabinet names to `-` locally (needs JDK 21, cached under `%LOCALAPPDATA%\work-kit\sdf-pathfix`).

**Production compare.** Before `deploy prod` and `upload prod`, and in `--dryrun`, the kit imports production's copy of every item it would send, read-only, into a temporary checkout, and compares it with git:

| Production's copy | Result |
|---|---|
| same (files: bytes, line endings aside; objects: same meaning) | already live, no approval needed |
| an older version in `main`'s history | changed since: the approval must cover the PRs since that version |
| none (confirmed by a listing) | new: the approval must cover the PRs that made it |
| a version git never had | refused: "production holds a change git doesn't have", with a diff path |

An item the import leaves no copy of counts as new only when `object:list` or a `file:list` that shows files confirms production doesn't have it (a file in a new folder: the nearest parent folder's listing, with nothing under the new folder); otherwise the release stops ("could not import …; nothing was compared"). Objects are compared by meaning, but a row without a script ID (a role permission, a list value, a translation) that only production has is always a difference.

The approval is still `wk gate approved` on the released PR; other PRs and direct commits are covered by `Includes PR #K` / `Includes <sha>` lines on that comment. Sandbox needs no compare.

Opt in, in the repo's AGENTS.md:

```
- **SDF** (import, validate, deploy): only through `py tools/sdf.py` (work-kit's NetSuite SDF kit, settings in `sdf.toml`), never raw `suitecloud` project commands.
```

Tests: `py -m unittest discover -s toolkits/netsuite/tests`.
