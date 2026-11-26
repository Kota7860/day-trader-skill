#!/usr/bin/env python3
"""A local web interface for the day-trader skills. Standard library only.

    python3 trader-ui/server.py                 # http://127.0.0.1:8765, files in ./trader
    python3 trader-ui/server.py --port 9000 --dir ~/my-project

It runs the same tools the skills run (entry.py, exit.py, size.py, check.py,
ticket.py show, alerts.py quote) in the project folder and shows their output
and the files they write under trader/. It listens on 127.0.0.1 only.

It never places, approves or declines an order. There is no button for that.
An order still needs trader-risk's PASS, your typed yes in the Claude chat for
that one order, and Claude's own Allow once card.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOLS = {
    "entry": os.path.join(ROOT, "trader-entry", "entry.py"),
    "exit": os.path.join(ROOT, "trader-exit", "exit.py"),
    "budget": os.path.join(ROOT, "trader-budget", "size.py"),
    "risk": os.path.join(ROOT, "trader-risk", "check.py"),
    "tickets": os.path.join(ROOT, "trader-broker", "ticket.py"),
    "quote": os.path.join(ROOT, "trader-alerts", "alerts.py"),
    "forex": os.path.join(ROOT, "trader-forex", "forex.py"),
    "sessions": os.path.join(ROOT, "trader-forex", "forex.py"),
}
FILES = {"config": "config.json", "entry": "entry.json", "plan": "plan.json", "orders": "orders.json",
         "risk": os.path.join("state", "risk.json"), "portfolio": "portfolio.json",
         "broker": os.path.join("state", "broker.json"), "watchlist": "watchlist.json"}
SYMBOL = re.compile(r"^[A-Za-z][A-Za-z0-9./_=\-]{0,11}$")
NOW = re.compile(r"^\d{4}-\d{2}-\d{2} \d{1,2}:\d{2}$")
STOP = re.compile(r"^(or-mid|or-low|or-high|(price|pct|atr|pips):\d+(\.\d+)?)$")
TARGET = re.compile(r"^(\d+(\.\d+)?R|price:\d+(\.\d+)?)$")
PRICE = re.compile(r"^\d+(\.\d+)?$")
TIMEOUT = 90


class InputError(Exception):
    pass


def _pick(data, key, pattern=None, choices=None, required=False):
    v = str(data.get(key) or "").strip()
    if not v:
        if required:
            raise InputError("%s is required" % key)
        return None
    if choices is not None and v not in choices:
        raise InputError("%s must be one of %s" % (key, ", ".join(choices)))
    if pattern is not None and not pattern.match(v):
        raise InputError("%s %r is not valid" % (key, v))
    return v


def build_argv(step, data):
    """The exact command for one step, from whitelisted fields only. Never a shell."""
    if step not in TOOLS:
        raise InputError("unknown step %r" % step)
    py = [sys.executable, TOOLS[step]]
    now = _pick(data, "now", NOW)
    when = ["--now", now] if now else []
    if step == "entry":
        argv = py + ["check", _pick(data, "symbol", SYMBOL, required=True), "--write"]
        session = _pick(data, "session", choices=("ny", "london"))
        if session:
            argv += ["--session", session]
        side = _pick(data, "side", choices=("buy", "sell"))
        if side:
            argv += ["--side", side]
        return argv + when
    if step == "exit":
        argv = py + ["plan", "--write"]
        stop = _pick(data, "stop", STOP)
        target = _pick(data, "target", TARGET)
        if stop:
            argv += ["--stop", stop]
        if target:
            argv += ["--target", target]
        return argv + when
    if step == "budget":
        argv = py + ["--write"]
        limit = _pick(data, "limit", PRICE)
        if limit:
            argv += ["--limit", limit]
        return argv + when
    if step == "risk":
        return py + when
    if step == "tickets":
        return py + ["show"] + when
    if step == "sessions":
        return py + ["sessions"] + when
    if step == "forex":
        argv = py + ["scan"]
        session = _pick(data, "session", choices=("ny", "london"))
        group = _pick(data, "group", choices=("all", "majors", "crosses", "metals"))
        if session:
            argv += ["--session", session]
        if group:
            argv += ["--group", group]
        return argv + when
    syms = str(data.get("symbols") or "").split()
    if not syms or len(syms) > 12 or not all(SYMBOL.match(s) for s in syms):
        raise InputError("symbols: up to 12 tickers or pairs, separated by spaces")
    return py + ["quote"] + syms + ["--write"]


def run_step(step, data, project):
    argv = build_argv(step, data)
    try:
        p = subprocess.run(argv, cwd=project, capture_output=True, text=True, timeout=TIMEOUT)
        code, out, err = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        code, out, err = 1, "", "stopped after %d seconds (the data source may be slow)" % TIMEOUT
    shown = ["python3", os.path.relpath(argv[1], ROOT)] + argv[2:]
    return {"step": step, "command": " ".join(shown), "code": code, "stdout": out, "stderr": err}


def read_state(project):
    out = {}
    for key, rel in FILES.items():
        path = os.path.join(project, "trader", rel)
        if not os.path.exists(path):
            out[key] = None
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                out[key] = json.load(fh)
        except (OSError, ValueError) as e:
            out[key] = {"error": "could not read %s: %s" % (rel, e)}
    return out


def save_config(project, text):
    try:
        doc = json.loads(text)
    except ValueError as e:
        raise InputError("the config is not valid JSON: %s" % e)
    if not isinstance(doc, dict):
        raise InputError("the config must be a JSON object")
    path = os.path.join(project, "trader", "config.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return doc


def make_handler(project):
    with open(os.path.join(HERE, "index.html"), "rb") as fh:
        page = fh.read()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _local(self):
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def do_GET(self):
            if not self._local():
                return self._send(403, {"error": "local use only"})
            if self.path in ("/", "/index.html"):
                return self._send(200, page, "text/html; charset=utf-8")
            if self.path == "/api/state":
                return self._send(200, {"project": project, "files": read_state(project)})
            if self.path == "/api/example-config":
                with open(os.path.join(ROOT, "trader-budget", "config.example.json"), "rb") as fh:
                    return self._send(200, fh.read())
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._local() or "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._send(403, {"error": "local JSON requests only"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(min(n, 200000)) or b"{}")
                if not isinstance(data, dict):
                    raise InputError("send a JSON object")
                if self.path == "/api/run":
                    return self._send(200, run_step(str(data.get("step")), data, project))
                if self.path == "/api/config":
                    return self._send(200, {"config": save_config(project, str(data.get("text") or ""))})
                return self._send(404, {"error": "not found"})
            except (InputError, ValueError) as e:
                return self._send(400, {"error": str(e)})

    return Handler


def main(argv=None):
    ap = argparse.ArgumentParser(description="Local web interface for the day-trader skills.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--dir", default=".", help="the project folder that holds trader/ (default: here)")
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    a = ap.parse_args(argv)
    project = os.path.abspath(os.path.expanduser(a.dir))
    server = HTTPServer(("127.0.0.1", a.port), make_handler(project))
    url = "http://127.0.0.1:%d" % a.port
    print("Day Trader interface at %s (files in %s). Ctrl+C stops it." % (url, os.path.join(project, "trader")))
    print("It never places or approves an order: your yes stays in the Claude chat.")
    if not a.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
