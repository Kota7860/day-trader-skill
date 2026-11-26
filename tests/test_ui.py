"""The local web interface: it only builds whitelisted commands and never reaches an order tool."""

import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from _load import ROOT, load

ui = load("trader-ui", "server")


class Commands(unittest.TestCase):
    def test_entry_command(self):
        argv = ui.build_argv("entry", {"symbol": "EUR/USD", "session": "london", "side": "sell",
                                       "now": "2026-11-09 08:55"})
        self.assertEqual(argv[2:], ["check", "EUR/USD", "--write", "--session", "london", "--side", "sell",
                                    "--now", "2026-11-09 08:55"])
        self.assertTrue(argv[1].endswith(os.path.join("trader-entry", "entry.py")))

    def test_forex_scan_and_sessions(self):
        argv = ui.build_argv("forex", {"session": "london", "group": "majors", "now": "2026-11-23 10:00"})
        self.assertTrue(argv[1].endswith(os.path.join("trader-forex", "forex.py")))
        self.assertEqual(argv[2:], ["scan", "--session", "london", "--group", "majors", "--now", "2026-11-23 10:00"])
        self.assertEqual(ui.build_argv("sessions", {})[2:], ["sessions"])
        with self.assertRaises(ui.InputError):
            ui.build_argv("forex", {"group": "everything"})

    def test_bad_input_is_refused_before_anything_runs(self):
        for step, data in (("entry", {"symbol": "EURUSD; rm -rf /"}), ("entry", {}),
                           ("entry", {"symbol": "EURUSD", "session": "tokyo"}),
                           ("exit", {"stop": "--config=/etc/passwd"}), ("budget", {"limit": "1e9"}),
                           ("quote", {"symbols": ""}), ("entry", {"symbol": "TSLA", "now": "tomorrow"}),
                           ("approve", {}), ("place", {})):
            with self.assertRaises(ui.InputError, msg=(step, data)):
                ui.build_argv(step, data)

    def test_no_step_can_approve_or_place(self):
        argvs = [ui.build_argv("tickets", {}), ui.build_argv("risk", {}), ui.build_argv("budget", {}),
                 ui.build_argv("exit", {"stop": "pips:10", "target": "2R"}),
                 ui.build_argv("quote", {"symbols": "EURUSD XAUUSD"})]
        for argv in argvs:
            for word in ("approve", "decline", "result", "fill", "flatten"):
                self.assertNotIn(word, argv)
        self.assertEqual(ui.build_argv("tickets", {})[2:], ["show"])


class Server(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.httpd = ui.HTTPServer(("127.0.0.1", 0), ui.make_handler(self.tmp))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_port

    def call(self, path, body=None, host=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        if host:
            req.add_header("Host", host)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_page_and_empty_state(self):
        code, page = self.call("/")
        self.assertEqual(code, 200)
        self.assertIn(b"never places or approves an order", page)
        code, body = self.call("/api/state")
        self.assertEqual(json.loads(body)["files"]["plan"], None)

    def test_save_config_and_read_it_back(self):
        code, _ = self.call("/api/config", {"text": json.dumps({"budget": 5000, "allow_short": False})})
        self.assertEqual(code, 200)
        state = json.loads(self.call("/api/state")[1])
        self.assertEqual(state["files"]["config"]["budget"], 5000)
        code, body = self.call("/api/config", {"text": "not json"})
        self.assertEqual(code, 400)

    def test_a_step_runs_in_the_project_folder(self):
        plan = {"symbol": "EURUSD", "side": "buy", "date": "2026-11-09", "setup": "orb", "entry": 1.1202,
                "stop": 1.11976, "target": 1.12086, "flat_by": "16:55", "market": "forex", "session": "ny"}
        os.makedirs(os.path.join(self.tmp, "trader"))
        with open(os.path.join(self.tmp, "trader", "plan.json"), "w") as fh:
            json.dump(plan, fh)
        with open(os.path.join(self.tmp, "trader", "config.json"), "w") as fh:
            json.dump({"budget": 5000}, fh)
        code, body = self.call("/api/run", {"step": "budget", "now": "2026-11-09 08:55"})
        res = json.loads(body)
        self.assertEqual((code, res["code"]), (200, 0), res)
        self.assertIn("3,347 units of EURUSD", res["stdout"])
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "trader", "orders.json")))

    def test_requests_for_another_host_are_refused(self):
        self.assertEqual(self.call("/api/state", host="evil.example")[0], 403)
        self.assertEqual(self.call("/api/run", {"step": "risk"}, host="evil.example")[0], 403)


if __name__ == "__main__":
    unittest.main()
