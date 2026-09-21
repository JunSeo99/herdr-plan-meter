import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import meter  # noqa: E402

NOW = 1_800_000_000.0
EN = meter.View({"lang": "en"})
KO = meter.View({"lang": "ko", "icons": "nerd"})

# Trimmed response shapes as the endpoints returned them in 2026-09.
CLAUDE = {
    "five_hour": {"utilization": 8.0, "resets_at": "2027-01-15T08:40:00.604354+00:00"},
    "seven_day": {"utilization": 21.0, "resets_at": "2027-01-18T15:00:00.604385+00:00"},
    "seven_day_opus": None,
    "limits": [
        {"kind": "session", "percent": 8, "resets_at": "2027-01-15T08:40:00+00:00", "scope": None},
        {"kind": "weekly_scoped", "percent": 18, "resets_at": "2027-01-18T15:00:00+00:00",
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}},
    ],
}
CODEX = {
    "plan_type": "plus",
    "rate_limit": {
        "primary_window": {"used_percent": 0, "limit_window_seconds": 18000, "reset_at": 1789463537},
        "secondary_window": {"used_percent": 4, "limit_window_seconds": 604800, "reset_at": 1789811340},
    },
}
# A seat metered on credits rather than on time: every window comes back null and
# spend carries the only reading. Numbers are made up, the shape is not.
CLAUDE_CREDITS = {
    "five_hour": None,
    "seven_day": None,
    "limits": [],
    "spend": {
        "used": {"amount_minor": 4200, "currency": "USD", "exponent": 2},
        "limit": {"amount_minor": 10000, "currency": "USD", "exponent": 2},
        "percent": 42, "severity": "warning", "enabled": True,
    },
}
NO_LOGIN = {"error": {"code": "nologin"}}


def reading(windows, fetched_at=NOW - 30, **extra):
    return {"windows": windows, "fetched_at": fetched_at, **extra}


class Parsing(unittest.TestCase):
    def test_claude_windows(self):
        wins = meter.parse_claude(CLAUDE)
        self.assertEqual([(w["kind"], w.get("model"), w["used"]) for w in wins],
                         [("session", None, 8.0), ("weekly", None, 21.0), ("weekly", "Fable", 18)])
        self.assertAlmostEqual(wins[0]["resets_at"], 1800002400.604354)

    def test_claude_credits_window(self):
        wins = meter.parse_claude(CLAUDE_CREDITS)
        self.assertEqual([(w["kind"], w["used"], w["resets_at"]) for w in wins],
                         [("credits", 42, None)])

    def test_claude_credits_ignored_when_disabled(self):
        # A plan with real windows reports enabled false; nothing is added for it.
        self.assertEqual(
            meter.parse_claude({**CLAUDE, "spend": {"percent": 0, "enabled": False}}),
            meter.parse_claude(CLAUDE))

    def test_claude_credits_needs_a_number(self):
        with self.assertRaises(meter.FetchError) as ctx:
            meter.parse_claude({"five_hour": None, "seven_day": None, "limits": [],
                                "spend": {"enabled": True, "percent": None}})
        self.assertEqual(ctx.exception.code, "format")

    def test_credits_label(self):
        t = meter.TEXT["en"]
        self.assertEqual(meter.label({"kind": "credits"}, t), "Credits")

    def test_claude_unknown_shape(self):
        with self.assertRaises(meter.FetchError) as ctx:
            meter.parse_claude({"something_new": {}})
        self.assertEqual(ctx.exception.code, "format")

    def test_codex_windows(self):
        wins = meter.parse_codex(CODEX)
        self.assertEqual([(w["kind"], w["used"], w["resets_at"]) for w in wins],
                         [("session", 0, 1789463537), ("weekly", 4, 1789811340)])

    def test_window_kind(self):
        self.assertEqual(meter.window_kind(None, "weekly"), {"kind": "weekly"})
        self.assertEqual(meter.window_kind(3 * 3600, "session"), {"kind": "hours", "n": 3})
        self.assertEqual(meter.window_kind(30 * 86400, "weekly"), {"kind": "days", "n": 30})

    def test_to_epoch(self):
        self.assertEqual(meter.to_epoch(1789463537), 1789463537)
        self.assertEqual(meter.to_epoch(1789463537000), 1789463537)
        self.assertEqual(meter.to_epoch("2026-09-15T00:00:00Z"), 1789430400)
        self.assertIsNone(meter.to_epoch("not a date"))

    def test_claude_plan(self):
        self.assertEqual(meter.claude_plan({"rateLimitTier": "default_claude_max_20x"}), "Max 20x")
        self.assertEqual(meter.claude_plan({"subscriptionType": "pro"}), "Pro")


class Http(unittest.TestCase):
    """http_json against a local server: status mapping, and no redirect ever carries the token."""

    @classmethod
    def setUpClass(cls):
        cls.leaked = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/leak":
                    cls.leaked.append(self.headers.get("Authorization"))
                status, headers, body = {
                    "/ok": (200, {}, b'{"a": 1}'),
                    "/bad": (200, {}, b"<html>"),
                    "/401": (401, {}, b""),
                    "/429": (429, {"Retry-After": "900"}, b""),
                    "/429-short": (429, {"Retry-After": "10"}, b""),
                    "/302": (302, {"Location": f"http://127.0.0.1:{cls.port}/leak"}, b""),
                }.get(self.path, (404, {}, b""))
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def get(self, path):
        return meter.http_json(f"http://127.0.0.1:{self.port}{path}", {"Authorization": "Bearer secret"}, "Codex")

    def error(self, path):
        with self.assertRaises(meter.FetchError) as ctx:
            self.get(path)
        return ctx.exception

    def test_ok(self):
        self.assertEqual(self.get("/ok"), {"a": 1})

    def test_status_mapping(self):
        self.assertEqual((self.error("/401").code, self.error("/401").params), ("expired", {"cli": "Codex"}))
        self.assertEqual(self.error("/bad").code, "format")
        self.assertEqual((self.error("/404").code, self.error("/404").params), ("http", {"status": 404}))

    def test_429_backs_off_at_least_five_minutes(self):
        self.assertEqual((self.error("/429").code, self.error("/429").retry_after), ("limited", 900))
        self.assertEqual(self.error("/429-short").retry_after, 300)

    def test_redirect_is_not_followed(self):
        err = self.error("/302")
        self.assertEqual((err.code, err.params), ("http", {"status": 302}))
        self.assertEqual(self.leaked, [])


class Cache(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        for name, value in (("CACHE_DIR", d), ("CACHE", d / "usage.json"), ("LOCK", d / "fetch.lock")):
            patcher = mock.patch.object(meter, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def fetchers(self, **fns):
        return mock.patch.dict(meter.FETCH, fns)

    def test_failure_keeps_last_good_reading(self):
        good = [{"kind": "weekly", "used": 21, "resets_at": NOW}]
        meter.save({"claude": {"windows": good, "fetched_at": 1, "attempt_at": 1, "retry_at": 0}})

        def down():
            raise meter.FetchError("network")
        with self.fetchers(claude=down, codex=lambda: {"plan": "Plus", "windows": good}):
            data = meter.refresh(0)
        self.assertEqual(data["claude"]["windows"], good)
        self.assertEqual(data["claude"]["error"], {"code": "network"})
        self.assertEqual(data["codex"]["windows"], good)
        self.assertEqual(meter.load(), data)

    def test_attempt_is_recorded_before_the_network_call(self):
        seen = {}

        def spy():
            seen["attempt_at"] = json.loads(meter.CACHE.read_text())["claude"]["attempt_at"]
            return {"plan": "", "windows": []}
        start = time.time()
        with self.fetchers(claude=spy, codex=lambda: {"plan": "", "windows": []}):
            meter.refresh(0)
        self.assertGreaterEqual(seen["attempt_at"], start)

    def test_429_suppresses_the_next_fetch(self):
        calls = []

        def limited():
            calls.append(1)
            raise meter.FetchError("limited", 300)
        with self.fetchers(claude=limited, codex=lambda: {"plan": "", "windows": []}):
            data = meter.refresh(0)
            meter.refresh(0)
        self.assertGreaterEqual(data["claude"]["retry_at"], time.time() + 299)
        self.assertEqual(len(calls), 1)

    def test_hung_fetch_gives_up_at_the_deadline(self):
        with mock.patch.object(meter, "FETCH_DEADLINE", 0.2), \
                self.fetchers(claude=lambda: time.sleep(5), codex=lambda: {"plan": "", "windows": []}):
            start = time.time()
            data = meter.refresh(0)
        self.assertLess(time.time() - start, 2)
        self.assertEqual(data["claude"]["error"], {"code": "network"})

    def test_damaged_cache_is_ignored(self):
        for text in ("[]", '{"claude": "x", "codex": {"windows": {"used": 1}, "error": "old"}}', "{"):
            meter.CACHE.write_text(text)
            data = meter.load()
            self.assertEqual(meter.bar_text(data, NOW, EN), "Claude –   Codex –")
            meter.panel_lines(data, NOW, "detail", False, 62, EN)


class Rendering(unittest.TestCase):
    def test_bar_line(self):
        data = {"claude": reading([{"kind": "session", "used": 8, "resets_at": NOW + 3600},
                                   {"kind": "weekly", "used": 21, "resets_at": NOW + 3 * 86400 + 10 * 3600}]),
                "codex": reading([{"kind": "weekly", "used": 4, "resets_at": NOW + 4 * 86400 + 5 * 3600}])}
        self.assertEqual(meter.bar_text(data, NOW, EN), "Claude 21% used 3d 10h   Codex 4% used 4d 5h")
        self.assertEqual(meter.bar_text(data, NOW, KO), "\uec82  21% 사용 3d 10h   \uec81  4% 사용 4d 5h")

    def test_stale_reading_is_marked(self):
        data = {"claude": reading([{"kind": "weekly", "used": 21, "resets_at": NOW + 3600}], fetched_at=NOW - 3600),
                "codex": NO_LOGIN}
        self.assertEqual(meter.bar_text(data, NOW, EN), "Claude ~21% used 1h 0m")

    def test_passed_reset_counts_as_zero(self):
        data = {"claude": reading([{"kind": "session", "used": 90, "resets_at": NOW - 1}]), "codex": NO_LOGIN}
        self.assertEqual(meter.bar_text(data, NOW, EN), "Claude 0% used")

    def test_provider_without_login_is_hidden(self):
        data = {"claude": reading([{"kind": "weekly", "used": 5, "resets_at": None}]), "codex": NO_LOGIN}
        self.assertEqual(meter.bar_text(data, NOW, EN), "Claude 5% used")

    def test_provider_with_other_error_stays(self):
        data = {"codex": {"error": {"code": "network"}}}
        self.assertEqual(meter.bar_text(data, NOW, EN), "Claude –   Codex –")

    def test_left(self):
        self.assertEqual(meter.left(NOW + 59, NOW), "0m")
        self.assertEqual(meter.left(NOW + 2 * 3600 + 13 * 60, NOW), "2h 13m")
        self.assertEqual(meter.left(NOW + 86400 + 3600, NOW), "1d 1h")

    def test_error_text(self):
        self.assertEqual(meter.error_text({"code": "http", "status": 500}, EN.t), "HTTP 500")
        self.assertEqual(meter.error_text({"code": "expired", "cli": "Codex"}, KO.t),
                         "로그인 만료 — Codex를 실행하면 갱신돼요")
        self.assertEqual(meter.error_text({"code": "from-the-future"}, EN.t), "Unexpected error: from-the-future")
        self.assertEqual(meter.error_text({"code": "expired"}, EN.t), "expired")

    def test_unknown_window_kind(self):
        self.assertEqual(meter.label({"kind": "monthly"}, EN.t), "monthly")

    def test_panel_fits_width(self):
        data = meter.demo_data(NOW)
        for view in (EN, KO):
            for line in meter.panel_lines(data, NOW, "detail", False, 62, view):
                self.assertLessEqual(meter.width(line), 62, meter.ANSI.sub("", line))


class Setup(unittest.TestCase):
    def test_shim_and_snippet_quote_awkward_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "it's \"odd\" \\ dir"
            with mock.patch.dict(os.environ, {"HERDR_PLUGIN_CONFIG_DIR": str(cfg)}):
                shim = meter.write_shim()
            self.assertTrue(os.access(shim, os.X_OK))
            self.assertEqual(subprocess.run(["/bin/sh", "-n", str(shim)]).returncode, 0)
            text = meter.snippet(shim)
            entry = next(line for line in text.splitlines() if "tab_bar_right" not in line and "command" in line)
            command = json.loads(entry.split("command = ", 1)[1].split(", interval_seconds", 1)[0])
            self.assertEqual(subprocess.run(["/bin/sh", "-c", f"printf %s {command}"],
                                            capture_output=True, text=True).stdout, str(shim))
        self.assertIn('command = "junseo99.plan-meter.open"', text)

    def test_config_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "config.toml").write_text("lang = 'ko'   # comment\nicons = \"nerd\"\n")
            with mock.patch.dict(os.environ, {"HERDR_PLUGIN_CONFIG_DIR": tmp}):
                self.assertEqual(meter.load_config(), {"lang": "ko", "icons": "nerd"})


class EventHook(unittest.TestCase):
    def run_event(self, status):
        env = {"HERDR_PLUGIN_EVENT_JSON": f'{{"type": "pane.agent_status_changed", "agent_status": "{status}"}}'}
        with mock.patch.dict(os.environ, env), mock.patch.object(sys, "argv", ["meter.py", "event"]), \
                mock.patch.object(meter, "refresh") as refresh:
            meter.main()
        return refresh

    def test_turn_end_refreshes(self):
        for status in ("done", "idle", "blocked"):
            self.run_event(status).assert_called_once_with(meter.EVENT_MIN_AGE)

    def test_turn_start_does_not(self):
        self.run_event("working").assert_not_called()


if __name__ == "__main__":
    unittest.main()
