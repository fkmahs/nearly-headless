"""The NetSuite query kit against a fake SuiteQL endpoint: no network. The fake checks every request's OAuth signature
the way NetSuite does, and serves rows in pages."""

import base64
import hashlib
import hmac
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import query  # noqa: E402

SECRETS = {"ACCOUNT_ID": "1234567_SB1", "CONSUMER_KEY": "ck-31415", "CONSUMER_SECRET": "cs+/=",
           "TOKEN_ID": "tk-27182", "TOKEN_SECRET": "ts&x"}
ENV_TEXT = "".join(f"NETSUITE_SB_{k}={v}\n" for k, v in SECRETS.items())


class FakeNetSuite:
    """Stands in for urlopen: verifies the signature, then serves `rows` by limit/offset."""

    def __init__(self, rows, fail_first=()):
        self.rows, self.fail_first, self.calls = rows, list(fail_first), []

    def __call__(self, request, timeout=None):
        url = urllib.parse.urlsplit(request.full_url)
        query_args = dict(urllib.parse.parse_qsl(url.query))
        self.calls.append(query_args)
        assert request.get_method() == "POST"
        assert url.netloc == "1234567-sb1.suitetalk.api.netsuite.com"
        verify_signature(request, f"https://{url.netloc}{url.path}", query_args)
        if self.fail_first:
            code = self.fail_first.pop(0)
            raise urllib.error.HTTPError(request.full_url, code, "busy", {}, io.BytesIO(b"{}"))
        limit, offset = int(query_args["limit"]), int(query_args["offset"])
        items = [dict(r, links=[]) for r in self.rows[offset:offset + limit]]
        body = {"items": items, "count": len(items), "offset": offset, "totalResults": len(self.rows),
                "hasMore": offset + len(items) < len(self.rows)}
        return io.BytesIO(json.dumps(body).encode())


def verify_signature(request, base_url, query_args):
    """NetSuite's side of OAuth 1.0a, written out independently of query.py."""
    header = request.get_header("Authorization")
    assert header.startswith("OAuth ")
    fields = {k: urllib.parse.unquote(v.strip('"'))
              for k, v in (p.strip().split("=", 1) for p in header[6:].split(","))}
    assert fields.pop("realm") == "1234567_SB1"
    assert fields["oauth_signature_method"] == "HMAC-SHA256"
    signature = fields.pop("oauth_signature")
    enc = lambda s: urllib.parse.quote(s, safe="~")  # noqa: E731
    params = "&".join(f"{k}={v}" for k, v in sorted((enc(k), enc(v)) for k, v in {**query_args, **fields}.items()))
    base = f"POST&{enc(base_url)}&{enc(params)}"
    key = f"{enc(SECRETS['CONSUMER_SECRET'])}&{enc(SECRETS['TOKEN_SECRET'])}"
    expected = base64.b64encode(hmac.new(key.encode(), base.encode(), hashlib.sha256).digest()).decode()
    assert hmac.compare_digest(signature, expected), "bad signature"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / ".git").mkdir()
        (self.tmp / ".env").write_text(ENV_TEXT, encoding="utf-8")
        self.cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, self.cwd)
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("NETSUITE_ENV_FILE", None)

    def run_cli(self, *argv, rows=(), fail_first=()):
        fake = FakeNetSuite(list(rows), fail_first)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(query.urllib.request, "urlopen", fake), mock.patch.object(query.time, "sleep"), \
                redirect_stdout(out), redirect_stderr(err):
            code = query.main(list(argv))
        return code, out.getvalue(), err.getvalue(), fake


class Signing(unittest.TestCase):
    def test_known_answer(self):
        # Base string spelled out by hand for one fixed request.
        params = {"limit": "5", "offset": "0", "oauth_consumer_key": "ck", "oauth_nonce": "n",
                  "oauth_signature_method": "HMAC-SHA256", "oauth_timestamp": "1", "oauth_token": "tk",
                  "oauth_version": "1.0"}
        base = ("POST&https%3A%2F%2Fa.example%2Fq&limit%3D5%26oauth_consumer_key%3Dck%26oauth_nonce%3Dn"
                "%26oauth_signature_method%3DHMAC-SHA256%26oauth_timestamp%3D1%26oauth_token%3Dtk"
                "%26oauth_version%3D1.0%26offset%3D0")
        expected = base64.b64encode(hmac.new(b"cs&ts", base.encode(), hashlib.sha256).digest()).decode()
        self.assertEqual(query.oauth_signature("post", "HTTPS://A.example/q", params, "cs", "ts"), expected)

    def test_header_verifies_and_hides_nothing_secret(self):
        url = query.suiteql_url(SECRETS["ACCOUNT_ID"])
        header = query.authorization_header(SECRETS, "POST", url, {"limit": "1", "offset": "0"})
        request = mock.Mock(get_header=lambda name: header)
        verify_signature(request, url, {"limit": "1", "offset": "0"})
        self.assertNotIn("cs+/=", header)
        self.assertNotIn("ts&x", header)

    def test_account_forms(self):
        self.assertEqual(query.account_host("1234567_SB1"), "1234567-sb1")
        self.assertEqual(query.account_realm("1234567-sb1"), "1234567_SB1")


class Paging(Base):
    def test_pages_through_every_row(self):
        rows = [{"id": str(i)} for i in range(2500)]
        code, out, err, fake = self.run_cli("--env", "sb", "--out", "r.json", "SELECT id FROM t ORDER BY id", rows=rows)
        self.assertEqual(code, 0, err)
        self.assertEqual([(c["offset"], c["limit"]) for c in fake.calls], [("0", "1000"), ("1000", "1000"), ("2000", "1000")])
        self.assertEqual(json.loads((self.tmp / "r.json").read_text(encoding="utf-8")), rows)

    def test_max_rows_stops_early(self):
        rows = [{"id": str(i)} for i in range(2500)]
        code, *_, fake = self.run_cli("--env", "sb", "--max-rows", "1500", "--out", "r.csv",
                                      "SELECT id FROM t ORDER BY id", rows=rows)
        self.assertEqual(code, 0)
        self.assertEqual([c["limit"] for c in fake.calls], ["1000", "500"])
        self.assertEqual(len((self.tmp / "r.csv").read_text(encoding="utf-8").splitlines()), 1501)

    def test_multi_page_without_order_by_is_refused(self):
        code, _, err, _ = self.run_cli("--env", "sb", "SELECT id FROM t", rows=[{"id": "1"}] * 1001)
        self.assertEqual(code, 1)
        self.assertIn("ORDER BY", err)

    def test_more_than_netsuite_pages_is_refused(self):
        rows = [{"id": "1"}] * (query.ROW_CAP + 1)
        code, _, err, _ = self.run_cli("--env", "sb", "SELECT id FROM t ORDER BY id", rows=rows)
        self.assertEqual(code, 1)
        self.assertIn("Split it", err)

    def test_busy_server_is_retried_then_reported(self):
        code, *_ = self.run_cli("--env", "sb", "SELECT 1 FROM dual", rows=[{"x": 1}], fail_first=[429, 503])
        self.assertEqual(code, 0)
        code, _, err, _ = self.run_cli("--env", "sb", "SELECT 1 FROM dual", fail_first=[400])
        self.assertEqual(code, 1)
        self.assertIn("HTTP 400", err)


class Output(Base):
    def test_csv_keeps_every_column_and_drops_links(self):
        rows = [{"id": "1", "name": "A, Inc"}, {"id": "2", "memo": "x"}]   # NetSuite leaves null fields out
        code, *_ = self.run_cli("--env", "sb", "--out", "out/r.csv", "SELECT * FROM t", rows=rows)
        self.assertEqual(code, 0)
        data = (self.tmp / "out" / "r.csv").read_bytes()
        self.assertEqual(data, b'id,name,memo\n1,"A, Inc",\n2,,x\n')

    def test_table_to_screen(self):
        code, out, err, _ = self.run_cli("--env", "sb", "SELECT id FROM t", rows=[{"id": "7"}])
        self.assertEqual(code, 0)
        self.assertIn("7", out)
        self.assertIn("[sb] 1 row(s)", err)


class Config(Base):
    def test_prefix_and_worktree_lookup(self):
        (self.tmp / ".env").write_text(ENV_TEXT.replace("NETSUITE_SB_", "ACME_SB_"), encoding="utf-8")
        nested = self.tmp / ".claude" / "worktrees" / "w"
        nested.mkdir(parents=True)
        os.chdir(nested)
        self.assertEqual(query.find_env_file(), self.tmp / ".env")
        code, *_ = self.run_cli("--env", "sb", "--prefix", "ACME_SB_", "SELECT 1 FROM dual", rows=[{"x": 1}])
        self.assertEqual(code, 0)

    def test_missing_keys_named_not_valued(self):
        (self.tmp / ".env").write_text("NETSUITE_SB_ACCOUNT_ID=1\n", encoding="utf-8")
        code, _, err, _ = self.run_cli("--env", "sb", "SELECT 1 FROM dual")
        self.assertEqual(code, 1)
        self.assertIn("NETSUITE_SB_TOKEN_SECRET", err)

    def test_check_prints_no_secret(self):
        code, out, *_ = self.run_cli("--env", "sb", "--check", rows=[{"id": "1"}])
        self.assertEqual(code, 0)
        self.assertIn("Connection OK", out)
        for value in SECRETS.values():
            self.assertNotIn(value, out)

    def test_only_reads(self):
        for sql in ("DELETE FROM t", "SELECT 1; DROP TABLE t", "  "):
            with self.assertRaises(query.QueryError):
                query.ensure_read_only(sql)
        self.assertEqual(query.ensure_read_only("select ';' from t;"), "select ';' from t")


if __name__ == "__main__":
    unittest.main()
