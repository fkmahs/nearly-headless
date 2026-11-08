#!/usr/bin/env python3
"""Read-only SuiteQL over NetSuite's REST API, for any repo that opts in.

Runs one SELECT/WITH statement through the SuiteTalk REST query endpoint, signed with token-based auth (OAuth 1.0a,
HMAC-SHA256), pages through the whole result and writes it to a CSV or JSON file. Standard library only. Credentials
come from the calling repo's git-ignored .env and are never printed. Setup and .env keys: README.md.

    py <work-kit>/toolkits/netsuite/query.py --env sb "SELECT id FROM subsidiary FETCH FIRST 1 ROWS ONLY"
    py <work-kit>/toolkits/netsuite/query.py --env prod --file q.sql --out out/result.csv
    py <work-kit>/toolkits/netsuite/query.py --env sb --check
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

FIELDS = ("ACCOUNT_ID", "CONSUMER_KEY", "CONSUMER_SECRET", "TOKEN_ID", "TOKEN_SECRET")
PAGE_SIZE = 1000          # NetSuite's largest page for the SuiteQL endpoint
ROW_CAP = 100_000         # NetSuite stops offset paging here; split larger pulls with a WHERE on a key
ORDERED = re.compile(r"\border\s+by\b", re.IGNORECASE)
CHECK_QUERY = "SELECT id FROM currency FETCH FIRST 1 ROWS ONLY"
RETRY_CODES = (429, 500, 502, 503, 504)   # 429: the account's concurrency limit; worth a wait and a retry
RETRIES = 4


class QueryError(Exception):
    """A problem worth reporting without a traceback."""


# --- configuration -------------------------------------------------------------------------------------------------

def env_prefix(env: str) -> str:
    """--env sb -> NETSUITE_SB_ (the default naming in a repo's .env)."""
    return f"NETSUITE_{re.sub(r'[^A-Za-z0-9]', '_', env).upper()}_"


def find_env_file(start: Path | None = None) -> Path:
    """NETSUITE_ENV_FILE if set, else the nearest .env from the working folder up to the repo's main checkout, so one
    .env in the main checkout serves every worktree beneath it."""
    if os.environ.get("NETSUITE_ENV_FILE"):
        return Path(os.environ["NETSUITE_ENV_FILE"])
    start = (start or Path.cwd()).resolve()
    for folder in (start, *start.parents):
        if (folder / ".env").is_file() or (folder / ".git").is_dir():   # the main checkout is the last stop
            return folder / ".env"
    return start / ".env"


def load_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines; blank lines and # comments are ignored, surrounding quotes stripped."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().removeprefix("export "), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_config(prefix: str, env_file: Path) -> dict[str, str]:
    """The five token-based-auth settings under a prefix; the process environment overrides the .env."""
    file_values = load_env_file(env_file)
    config = {f: os.environ.get(prefix + f) or file_values.get(prefix + f, "") for f in FIELDS}
    missing = [prefix + f for f in FIELDS if not config[f]]
    if missing:
        raise QueryError(f"missing in {env_file}: {', '.join(missing)} (see the netsuite toolkit README)")
    return config


def account_host(account_id: str) -> str:
    """1234567_SB1 -> 1234567-sb1 (NetSuite's hostname form)."""
    return account_id.strip().lower().replace("_", "-")


def account_realm(account_id: str) -> str:
    """1234567-sb1 -> 1234567_SB1 (NetSuite's OAuth realm form)."""
    return account_id.strip().upper().replace("-", "_")


def suiteql_url(account_id: str) -> str:
    return f"https://{account_host(account_id)}.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql"


# --- read-only guard -----------------------------------------------------------------------------------------------

def ensure_read_only(sql: str) -> str:
    """The statement without a trailing semicolon; raises unless it is a single SELECT/WITH."""
    scrubbed = re.sub(r"'(?:[^']|'')*'", "''", sql)        # keywords inside strings and comments don't count
    scrubbed = re.sub(r"--[^\n]*", " ", scrubbed)
    scrubbed = re.sub(r"/\*.*?\*/", " ", scrubbed, flags=re.S)
    body = scrubbed.strip().rstrip(";").strip()
    if not body:
        raise QueryError("empty query")
    if ";" in body:
        raise QueryError("only one statement is allowed")
    first = body.split(None, 1)[0].upper()
    if first not in ("SELECT", "WITH"):
        raise QueryError(f"read-only: a statement must start with SELECT or WITH, not {first}")
    return sql.strip().rstrip(";").strip()


# --- OAuth 1.0a signing --------------------------------------------------------------------------------------------

def _pct(value: str) -> str:
    return urllib.parse.quote(str(value), safe="~")


def oauth_signature(method: str, url: str, params: dict[str, str], consumer_secret: str, token_secret: str) -> str:
    """RFC 5849 section 3.4 HMAC-SHA256 signature over the normalised request."""
    parts = urllib.parse.urlsplit(url)
    base_url = f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path}"
    pairs = sorted((_pct(k), _pct(v)) for k, v in params.items())
    param_string = "&".join(f"{k}={v}" for k, v in pairs)
    base_string = "&".join((method.upper(), _pct(base_url), _pct(param_string)))
    key = f"{_pct(consumer_secret)}&{_pct(token_secret)}"
    return base64.b64encode(hmac.new(key.encode(), base_string.encode(), hashlib.sha256).digest()).decode()


def authorization_header(config: dict[str, str], method: str, url: str, query: dict[str, str]) -> str:
    oauth = {
        "oauth_consumer_key": config["CONSUMER_KEY"],
        "oauth_token": config["TOKEN_ID"],
        "oauth_signature_method": "HMAC-SHA256",
        "oauth_timestamp": str(int(time.time())),
        "oauth_nonce": secrets.token_hex(16),
        "oauth_version": "1.0",
    }
    oauth["oauth_signature"] = oauth_signature(method, url, {**query, **oauth},
                                               config["CONSUMER_SECRET"], config["TOKEN_SECRET"])
    fields = [f'realm="{account_realm(config["ACCOUNT_ID"])}"'] + [f'{k}="{_pct(v)}"' for k, v in oauth.items()]
    return "OAuth " + ", ".join(fields)


# --- querying ------------------------------------------------------------------------------------------------------

def _error_detail(err: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(err.read().decode("utf-8", "replace"))
        details = body.get("o:errorDetails") or []
        return "; ".join(d.get("detail", "") for d in details) or body.get("title", "") or str(body)
    except (ValueError, AttributeError):
        return str(err.reason)


def fetch_page(config: dict[str, str], sql: str, limit: int, offset: int, wait=time.sleep) -> dict:
    """One page, signed afresh on each try; busy or failing servers get a few retries with a growing wait."""
    url = suiteql_url(config["ACCOUNT_ID"])
    query = {"limit": str(limit), "offset": str(offset)}
    for attempt in range(RETRIES + 1):
        request = urllib.request.Request(
            f"{url}?{urllib.parse.urlencode(query)}", data=json.dumps({"q": sql}).encode(), method="POST",
            headers={"Authorization": authorization_header(config, "POST", url, query),
                     "Content-Type": "application/json", "Prefer": "transient"})
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                return json.load(response)
        except urllib.error.HTTPError as err:
            if err.code in RETRY_CODES and attempt < RETRIES:
                wait(2 ** attempt)
                continue
            raise QueryError(f"NetSuite returned HTTP {err.code}: {_error_detail(err)}") from None
        except urllib.error.URLError as err:
            raise QueryError(f"could not reach {account_host(config['ACCOUNT_ID'])}: {err.reason}") from None
    raise AssertionError("unreachable")


def run_query(config: dict[str, str], sql: str, max_rows: int | None = None, progress=None) -> tuple[list[dict], int | None]:
    """Every row (or the first max_rows), page by page; returns (rows, NetSuite's totalResults)."""
    cap = min(max_rows or ROW_CAP, ROW_CAP)
    rows: list[dict] = []
    total = None
    while len(rows) < cap:
        page = fetch_page(config, sql, min(PAGE_SIZE, cap - len(rows)), len(rows))
        total = page.get("totalResults", total)
        if not rows and page.get("hasMore"):
            if not ORDERED.search(sql):   # without a fixed order NetSuite may repeat or skip rows between pages
                raise QueryError(f"the query returns {total} rows over several pages but has no ORDER BY, so pages "
                                 "may repeat or skip rows; add an ORDER BY on a unique key")
            if max_rows is None and total and total > ROW_CAP:
                raise QueryError(f"the query returns {total} rows; NetSuite pages through at most {ROW_CAP}. "
                                 "Split it with a WHERE on the ordered key, or set --max-rows")
        items = page.get("items", [])
        rows.extend({k: v for k, v in item.items() if k != "links"} for item in items)
        if progress:
            progress(len(rows), total)
        if not page.get("hasMore") or not items:
            break
    return rows, total


# --- output --------------------------------------------------------------------------------------------------------

def columns_of(rows: list[dict]) -> list[str]:
    """Every column in first-seen order; NetSuite leaves null fields out of a row."""
    cols: dict[str, None] = {}
    for row in rows:
        cols.update(dict.fromkeys(row))
    return list(cols)


def format_rows(rows: list[dict], fmt: str) -> str:
    if fmt == "json":
        return json.dumps(rows, indent=2, ensure_ascii=False)
    cols = columns_of(rows)
    if fmt == "csv":
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=cols, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        return buffer.getvalue().rstrip("\n")
    if not rows:
        return "(no rows)"
    cells = [[str(row.get(c, "")) for c in cols] for row in rows]
    widths = [min(60, max(len(c), *(len(r[i]) for r in cells))) for i, c in enumerate(cols)]
    fit = lambda value, w: (value if len(value) <= w else value[: w - 1] + "…").ljust(w)  # noqa: E731
    lines = [" | ".join(fit(c, w) for c, w in zip(cols, widths)), "-+-".join("-" * w for w in widths)]
    lines += [" | ".join(fit(v, w) for v, w in zip(r, widths)) for r in cells]
    return "\n".join(lines)


def output_format(out: Path | None, fmt: str | None) -> str:
    if fmt:
        return fmt
    if out and out.suffix.lower() in (".csv", ".json"):
        return out.suffix.lower()[1:]
    return "csv" if out else "table"


# --- CLI -----------------------------------------------------------------------------------------------------------

def check(prefix: str, env_file: Path) -> int:
    """Which keys are set (never their values), then one tiny read-only query."""
    file_values = load_env_file(env_file)
    print(f".env: {'found' if env_file.is_file() else 'NOT FOUND'} ({env_file})")
    for f in FIELDS:
        print(f"  {prefix + f:<32} {'set' if os.environ.get(prefix + f) or file_values.get(prefix + f) else 'MISSING'}")
    config = load_config(prefix, env_file)
    started = time.monotonic()
    rows, _ = run_query(config, CHECK_QUERY, 1)
    print(f"Connection OK: test query returned {len(rows)} row(s) in {time.monotonic() - started:.1f}s.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run read-only SuiteQL against NetSuite and write the result.")
    parser.add_argument("--env", required=True, help="which account in .env, e.g. sb or prod (keys NETSUITE_<ENV>_*)")
    parser.add_argument("--prefix", help="read the keys under this .env prefix instead, e.g. ACME_SB_")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("sql", nargs="?", help="a SuiteQL statement (SELECT/WITH only)")
    source.add_argument("--file", type=Path, help="read the statement from a .sql file")
    parser.add_argument("--check", action="store_true", help="show which .env keys are set and test the connection")
    parser.add_argument("--out", type=Path, help="write the rows to this file (.csv or .json) instead of the screen")
    parser.add_argument("--format", choices=("table", "csv", "json"), help="default: from --out's extension, else table")
    parser.add_argument("--max-rows", type=int, help="stop after this many rows (default: all)")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    prefix = args.prefix or env_prefix(args.env)
    env_file = find_env_file()
    try:
        if args.check:
            return check(prefix, env_file)
        sql = args.file.read_text(encoding="utf-8") if args.file else args.sql
        if not sql:
            parser.error("give a SuiteQL statement, --file or --check")
        sql = ensure_read_only(sql)
        config = load_config(prefix, env_file)
        started = time.monotonic()
        progress = None
        if args.out:
            progress = lambda n, total: print(f"  {n} of {total} rows", file=sys.stderr)  # noqa: E731
        rows, total = run_query(config, sql, max(1, args.max_rows) if args.max_rows else None, progress)
        fmt = output_format(args.out, args.format)
        text = format_rows(rows, fmt)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(text + "\n", encoding="utf-8", newline="\n")
            print(f"Wrote {len(rows)} row(s) to {args.out}")
        else:
            print(text)
        of_total = f" of {total}" if total is not None and total != len(rows) else ""
        print(f"[{args.env}] {len(rows)} row(s){of_total} in {time.monotonic() - started:.1f}s", file=sys.stderr)
        return 0
    except QueryError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
