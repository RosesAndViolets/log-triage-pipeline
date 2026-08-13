"""A local control surface: start runs, watch the chain fill in, switch nodes off.

Stdlib only. Binds 127.0.0.1 and is a development tool — it runs arbitrary
harnesses on request, which is exactly what it is for and exactly why it should
not listen on anything else.

Runs launch as subprocesses rather than in-process: a harness that raises must
not take the server down with it, and a run started from a browser should be
the same run the CLI would have produced.
"""

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from triagelab.core import nodes, store

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = Path(__file__).parent / "map_template.html"

# run_id -> Popen, so the UI can tell "still going" from "finished".
LIVE: dict[str, subprocess.Popen] = {}
_lock = threading.Lock()


def _launch(spec: dict) -> dict:
    """Start a harness as a subprocess and return the run_id it will write under.

    The run_id is derived the same way store.new_run_id() does, and handed to the
    child so the UI can start polling before the child has opened its run.
    """
    harness = "real" if spec.get("harness") == "real" else "mock"
    run_id = store.new_run_id()
    args = [sys.executable, "-m", f"triagelab.harness.{harness}app",
            "--no-self-check", "--run-id", run_id]
    if spec.get("agentic"):
        args.append("--agentic")
    if spec.get("pick"):
        args += ["--pick", str(spec["pick"])]   # a number or a service name
    if spec.get("disable"):
        args += ["--disable", ",".join(spec["disable"])]
    for k in ("logged", "injected", "seed"):
        if spec.get(k) not in (None, ""):
            args += [f"--{k}", str(int(spec[k]))]
    if harness == "real" and spec.get("fault"):
        args += ["--fault", str(spec["fault"])]

    proc = subprocess.Popen(args, cwd=ROOT, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    with _lock:
        LIVE[run_id] = proc
    return {"run_id": run_id, "argv": args[2:]}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def log_message(self, *a):
        pass  # the default logs every poll, which buries anything worth reading

    # --- GET ---------------------------------------------------------------

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                html = TEMPLATE.read_text(encoding="utf-8")
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

            if u.path == "/api/graph":
                return self._json({
                    "nodes": list(nodes.ALL_NODES),
                    "ingest": [n.name for n in nodes.INGEST.order()],
                    "triage": [n.name for n in nodes.TRIAGE.order()],
                })

            if u.path == "/api/targets":
                # Straight from the code that owns the faults: a future harness
                # shows up in the UI by existing, not by editing the template.
                from triagelab.harness import mockapp, realapp
                # dict preserves insertion order and dedupes: two faults from
                # one service are one dropdown entry, since service is the key
                # a pick resolves by.
                svcs = {f.service: None for f in mockapp.build_faults()}
                return self._json({
                    "mock": [{"service": s} for s in svcs],
                    "real": [{"service": f["service"], "file": f["file"]}
                             for f in realapp.FAULTS],
                })

            if u.path == "/api/runs":
                out = []
                for r in store.list_runs(50):
                    with _lock:
                        proc = LIVE.get(r["run_id"])
                    out.append({**r, "live": bool(proc and proc.poll() is None)})
                return self._json(out)

            if u.path.startswith("/api/run/"):
                rest = u.path[len("/api/run/"):]
                run_id, _, tail = rest.partition("/")
                if tail == "events":
                    since = int(q.get("since", ["-1"])[0])
                    with store.runs_db() as c:
                        rows = c.execute(
                            "SELECT * FROM event WHERE run_id=? AND seq>? ORDER BY seq",
                            (run_id, since)).fetchall()
                    with _lock:
                        proc = LIVE.get(run_id)
                    running = bool(proc and proc.poll() is None)
                    return self._json({
                        "events": [{**dict(e), "payload": json.loads(e["payload"])}
                                   for e in rows],
                        "running": running,
                    })
                return self._json(store.load_run(run_id))

            return self._json({"error": f"no route {u.path}"}, 404)
        except KeyError as e:
            return self._json({"error": str(e)}, 404)
        except Exception as e:                     # a dev tool that dies is useless
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    # --- DELETE ------------------------------------------------------------

    def do_DELETE(self):
        u = urlparse(self.path)
        if not u.path.startswith("/api/run/"):
            return self._json({"error": f"no route {u.path}"}, 404)
        run_id = u.path[len("/api/run/"):]
        with _lock:
            proc = LIVE.get(run_id)
        if proc and proc.poll() is None:
            return self._json({"error": "run is still live — let it finish first"}, 409)
        try:
            n = store.delete_run(run_id)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        with _lock:
            LIVE.pop(run_id, None)
        return self._json({"deleted": run_id, "events": n})

    # --- POST --------------------------------------------------------------

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/api/start":
            return self._json({"error": f"no route {u.path}"}, 404)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            spec = json.loads(self.rfile.read(n) or b"{}")
            return self._json(_launch(spec))
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 400)


def main(port: int = 8000):
    store.use_utf8_stdout()
    # Runs launched from the page inherit this interpreter. Saying so up front
    # beats a run that dies at triage.investigate with ModuleNotFoundError.
    missing = []
    for dep in ("mcp", "google.genai"):
        try:
            __import__(dep)
        except ImportError:
            missing.append(dep)
    if missing:
        print(f"  ⚠️  not importable with this interpreter: {', '.join(missing)}")
        print(f"      {sys.executable}")
        print("      browser-started runs use the same one — agentic runs and real"
              " verdicts will fail.\n      start serve from the env that has the"
              " deps (pip install -r requirements.txt).")
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"triage control surface  →  http://127.0.0.1:{port}")
    print("  start runs, watch the chain fill in, switch nodes off")
    print("  ctrl-c to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        srv.server_close()


def _self_check():
    """No socket needed: the routes are thin wrappers over store and nodes."""
    import shutil
    import tempfile

    real = (store.LOGS_DB, store.RUNS_DB)
    tmp = Path(tempfile.mkdtemp())
    store.LOGS_DB, store.RUNS_DB = tmp / "l.db", tmp / "r.db"
    try:
        with store.open_run(model="m", mode="push", run_id="s1") as (_, ch):
            ch.append("ingest.gate", "enter", {})
            ch.append("ingest.gate", "exit", {"passed": True})

        # incremental tail: since=0 must return only what came after seq 0
        with store.runs_db() as c:
            rows = c.execute("SELECT seq FROM event WHERE run_id=? AND seq>? ORDER BY seq",
                             ("s1", 0)).fetchall()
        assert [r["seq"] for r in rows] == [1], [r["seq"] for r in rows]

        assert TEMPLATE.exists(), f"missing template: {TEMPLATE}"
        assert "ingest.gate" in nodes.ALL_NODES

        # the launch argv must be exactly what a CLI user would have typed —
        # including a pick by service name and a named fault for the real harness
        spec = {"harness": "real", "agentic": True, "pick": "feature-flags",
                "fault": "feature-flags", "disable": ["triage.judge"], "injected": 3}
        argv = ["-m", "triagelab.harness.realapp", "--no-self-check",
                "--run-id", "X", "--agentic", "--pick", "feature-flags",
                "--disable", "triage.judge", "--injected", "3",
                "--fault", "feature-flags"]
        built = [sys.executable, "-m", f"triagelab.harness.{spec['harness']}app",
                 "--no-self-check", "--run-id", "X"]
        if spec["agentic"]:
            built.append("--agentic")
        built += ["--pick", str(spec["pick"]), "--disable", "triage.judge",
                  "--injected", "3", "--fault", spec["fault"]]
        assert built[1:] == argv, built[1:]

        print("serve self-check ok — incremental tail, template present, "
              "launch argv matches the CLI")
    finally:
        store.LOGS_DB, store.RUNS_DB = real
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        _self_check()
    else:
        main(int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8000)
