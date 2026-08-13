"""Two databases: what came in, and what happened.

`logs.db` holds ingested log records. `runs.db` holds the evidence chain — an
ordered, hash-linked record of every step the pipeline took. They are separate
files on purpose: one is input the pipeline reads, the other is testimony about
the pipeline itself, and mixing them makes it impossible to say which is which.

The chain is hash-linked so a replay can be checked rather than trusted: each
event binds to its predecessor, and `verify()` names the first row that does not
add up. Without that, "replay of a run" means "a drawing someone made".

Portability: every connection is closed explicitly rather than left to the
garbage collector. On Windows an open handle keeps a lock on the file, which
turns a leaked connection into a failure to delete or reopen the database.
"""

import hashlib
import json
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]   # the repo root, not this package
DATA = ROOT / "data"
LOGS_DB = DATA / "logs.db"   # module-level so self-checks can redirect them
RUNS_DB = DATA / "runs.db"

LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS log (
  id INTEGER PRIMARY KEY, run_id TEXT, ts TEXT, service TEXT, level TEXT,
  message TEXT, trace_id TEXT, error_class TEXT, stack TEXT, source TEXT);
CREATE INDEX IF NOT EXISTS ix_log_trace   ON log(run_id, trace_id);
CREATE INDEX IF NOT EXISTS ix_log_service ON log(run_id, service, level);
"""

RUN_SCHEMA = """
CREATE TABLE IF NOT EXISTS run (
  run_id TEXT PRIMARY KEY, started TEXT, ended TEXT,
  model TEXT, mode TEXT, condition TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS event (
  run_id TEXT, seq INTEGER, ts TEXT, node TEXT, kind TEXT,
  payload TEXT, prev_hash TEXT, hash TEXT, PRIMARY KEY (run_id, seq));
"""

KINDS = ("enter", "exit", "skip", "error", "emit", "tool", "verdict", "grade")


def use_utf8_stdout():
    """Make console output survive a Windows code page.

    cmd.exe defaults to cp1252, which cannot encode the emoji this pipeline
    prints — the run dies on a status line rather than on anything real.
    Call this from a __main__ block, never at import: rebinding another
    program's stdout because it imported us would be rude.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass  # already detached or not reconfigurable — not worth dying over


@contextmanager
def _open(path: Path, schema: str):
    path.parent.mkdir(parents=True, exist_ok=True)   # data/ on a fresh clone
    # timeout: on Windows a concurrent reader (the MCP subprocess) can hold the
    # file briefly; waiting beats raising "database is locked".
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        # WAL so mcp_server.py can read logs while this process writes them.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(schema)
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def logs_db():
    return _open(LOGS_DB, LOG_SCHEMA)


def runs_db():
    return _open(RUNS_DB, RUN_SCHEMA)


def new_run_id() -> str:
    return time.strftime("%Y%m%dT%H%M%S")


# --- logs ------------------------------------------------------------------


def write_log(run_id: str, entry: dict):
    """One ingested record. Called from the logging handler, so it stays cheap."""
    err = entry.get("error") or {}
    with logs_db() as c:
        c.execute(
            "INSERT INTO log (run_id, ts, service, level, message, trace_id,"
            " error_class, stack, source) VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, time.strftime("%Y-%m-%dT%H:%M:%S"), entry.get("serviceName"),
             entry.get("level"), entry.get("message"), entry.get("trace_id"),
             err.get("class"), err.get("stack"), err.get("source")),
        )


def query_logs(run_id: str = "", trace_id: str = "", service: str = "",
               level: str = "", limit: int = 40) -> list[dict]:
    """Indexed lookup. Filters combine with AND; empty string means 'any'."""
    where, args = [], []
    for col, val in (("run_id", run_id), ("trace_id", trace_id),
                     ("service", service), ("level", level.upper() if level else "")):
        if val:
            where.append(f"{col} = ?")
            args.append(val)
    sql = "SELECT * FROM log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, limit))
    with logs_db() as c:
        rows = c.execute(sql, args).fetchall()
    return [dict(r) for r in reversed(rows)]  # newest-N, but read oldest-first


def latest_run_id() -> str:
    """The run the MCP tools answer about when nobody says otherwise."""
    with runs_db() as c:
        r = c.execute("SELECT run_id FROM run ORDER BY started DESC, rowid DESC"
                      " LIMIT 1").fetchone()
    return r["run_id"] if r else ""


# --- the evidence chain ----------------------------------------------------


def _canon(payload) -> str:
    """Stable JSON so a hash over it means the same thing on both sides."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _digest(prev_hash: str, seq: int, node: str, kind: str, payload) -> str:
    return hashlib.sha256(
        f"{prev_hash}|{seq}|{node}|{kind}|{_canon(payload)}".encode("utf-8")
    ).hexdigest()


class Chain:
    """Append-only, hash-linked. Every event binds to the one before it."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        with runs_db() as c:
            r = c.execute(
                "SELECT seq, hash FROM event WHERE run_id=? ORDER BY seq DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        self.seq = (r["seq"] + 1) if r else 0
        self.prev = r["hash"] if r else ""

    def append(self, node: str, kind: str, payload=None) -> int:
        if kind not in KINDS:
            raise ValueError(f"unknown event kind: {kind!r} (expected one of {KINDS})")
        payload = payload if payload is not None else {}
        seq, h = self.seq, _digest(self.prev, self.seq, node, kind, payload)
        with runs_db() as c:
            c.execute(
                "INSERT INTO event (run_id, seq, ts, node, kind, payload, prev_hash, hash)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (self.run_id, seq, time.strftime("%Y-%m-%dT%H:%M:%S"), node, kind,
                 _canon(payload), self.prev, h),
            )
        self.seq, self.prev = seq + 1, h
        return seq


def verify(run_id: str) -> int | None:
    """Recompute the chain. Returns the first seq that fails, or None if intact.

    Catches an edited payload, a reordered row, and a deleted one — a gap breaks
    the link exactly as a rewrite does.
    """
    prev = ""
    with runs_db() as c:
        rows = c.execute(
            "SELECT * FROM event WHERE run_id=? ORDER BY seq", (run_id,)
        ).fetchall()
    for i, r in enumerate(rows):
        if r["seq"] != i or r["prev_hash"] != prev:
            return r["seq"]
        if _digest(prev, r["seq"], r["node"], r["kind"],
                   json.loads(r["payload"])) != r["hash"]:
            return r["seq"]
        prev = r["hash"]
    return None


@contextmanager
def open_run(model: str, mode: str, condition: dict | None = None, run_id: str = ""):
    """Bracket a run: the row is written on entry and closed out on exit.

    Status is set even when the body raises — a run that died is evidence too,
    and a chain that just stops with no reason is the least useful kind.
    """
    rid = run_id or new_run_id()
    with runs_db() as c:
        c.execute(
            "INSERT OR REPLACE INTO run (run_id, started, ended, model, mode,"
            " condition, status) VALUES (?,?,?,?,?,?,?)",
            (rid, time.strftime("%Y-%m-%dT%H:%M:%S"), None, model, mode,
             _canon(condition or {}), "running"),
        )
    chain = Chain(rid)
    try:
        yield rid, chain
    except BaseException as e:
        _close_run(rid, f"failed: {type(e).__name__}")
        raise
    else:
        _close_run(rid, "ok")


def _close_run(run_id: str, status: str):
    with runs_db() as c:
        c.execute("UPDATE run SET ended=?, status=? WHERE run_id=?",
                  (time.strftime("%Y-%m-%dT%H:%M:%S"), status, run_id))


# --- read path -------------------------------------------------------------


def load_run(run_id: str) -> dict:
    """Everything one run holds. The single read path — exporters use this."""
    with runs_db() as c:
        run = c.execute("SELECT * FROM run WHERE run_id=?", (run_id,)).fetchone()
        if not run:
            raise KeyError(f"no such run: {run_id}")
        events = c.execute(
            "SELECT * FROM event WHERE run_id=? ORDER BY seq", (run_id,)
        ).fetchall()
    return {
        "run": {**dict(run), "condition": json.loads(run["condition"] or "{}")},
        "events": [{**dict(e), "payload": json.loads(e["payload"])} for e in events],
        "logs": query_logs(run_id=run_id, limit=100000),
        "intact": verify(run_id) is None,
    }


def list_runs(limit: int = 20) -> list[dict]:
    """Recent runs, each labelled by what it was about rather than only when it ran.

    A list of timestamps all reading "push 175ev" tells you nothing about which
    error you are looking at, which is the only thing you actually pick a run by.
    """
    with runs_db() as c:
        rows = c.execute(
            "SELECT r.*, (SELECT COUNT(*) FROM event e WHERE e.run_id=r.run_id) AS events"
            " FROM run r ORDER BY started DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            # triage.select records what was picked; no row means nothing was triaged.
            sel = c.execute(
                "SELECT payload FROM event WHERE run_id=? AND node='triage.select'"
                " AND kind='exit' ORDER BY seq LIMIT 1", (r["run_id"],)).fetchone()
            d = dict(r)
            if sel:
                p = json.loads(sel["payload"])
                d["label"] = (f"{p.get('service', '?')} {p.get('error_class', '?')}"
                              f" {p.get('count', '?')}x")
            else:
                d["label"] = "not triaged"
            d["condition"] = json.loads(r["condition"] or "{}")
            out.append(d)
    return out


def _self_check():
    import gc
    import shutil
    import tempfile

    global LOGS_DB, RUNS_DB
    real = (LOGS_DB, RUNS_DB)
    tmp = Path(tempfile.mkdtemp())
    LOGS_DB, RUNS_DB = tmp / "logs.db", tmp / "runs.db"
    try:
        with open_run(model="m", mode="agentic", condition={"seed": 7}) as (rid, ch):
            write_log(rid, {"serviceName": "svc", "level": "INFO", "message": "lead",
                            "trace_id": "t1", "error": {}})
            write_log(rid, {"serviceName": "svc", "level": "ERROR",
                            "message": "boom — em dash, ünicode",  # must survive cp1252
                            "trace_id": "t1", "error": {"class": "KeyError", "stack": "s"}})
            ch.append("ingest.gate", "enter", {"level": "ERROR"})
            ch.append("ingest.gate", "exit", {"passed": True})
            ch.append("triage.investigate", "tool", {"name": "read_source"})

        # logs: filters combine, and the run scopes them
        assert len(query_logs(run_id=rid)) == 2
        assert len(query_logs(run_id=rid, level="error")) == 1
        got = query_logs(run_id=rid, trace_id="t1", level="ERROR")[0]["message"]
        assert got == "boom — em dash, ünicode", repr(got)  # round-tripped intact
        assert query_logs(run_id="nope") == []

        # a second run must not see the first one's logs — this is what
        # replaces truncating the store between runs
        with open_run(model="m", mode="push", run_id="run2") as (r2, ch2):
            write_log(r2, {"serviceName": "svc", "level": "ERROR", "message": "other",
                           "trace_id": "t9", "error": {}})
            ch2.append("ingest.gate", "enter", {})
        assert len(query_logs(run_id=rid)) == 2, "runs are leaking into each other"
        assert len(query_logs(run_id=r2)) == 1

        # chain: intact, ordered, closed out
        assert verify(rid) is None, "a chain we just wrote should verify"
        d = load_run(rid)
        assert d["intact"] and d["run"]["status"] == "ok"
        assert d["run"]["condition"] == {"seed": 7}
        assert [e["seq"] for e in d["events"]] == [0, 1, 2]
        assert d["events"][2]["payload"]["name"] == "read_source"

        # a failed run still closes, with a reason
        try:
            with open_run(model="m", mode="push", run_id="bad") as (_, ch3):
                ch3.append("ingest.gate", "enter", {})
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        with runs_db() as c:
            st = c.execute("SELECT status FROM run WHERE run_id='bad'").fetchone()["status"]
        assert st.startswith("failed: RuntimeError"), st

        assert Chain(rid).seq == 3, "a reopened chain must continue, not restart"
        try:
            Chain(rid).append("n", "not-a-kind", {})
            raise AssertionError("bad kind accepted")
        except ValueError:
            pass

        # tamper: rewrite one payload and the chain must name that seq
        with runs_db() as c:
            c.execute("UPDATE event SET payload=? WHERE run_id=? AND seq=1",
                      (_canon({"passed": False}), rid))
        assert verify(rid) == 1, f"tampering at seq 1 went undetected: {verify(rid)}"
        assert load_run(rid)["intact"] is False

        # a deleted row breaks the link too
        with runs_db() as c:
            c.execute("DELETE FROM event WHERE run_id=? AND seq=0", (rid,))
        assert verify(rid) == 1, "a gap should break the chain"

        # Leaked connections are a Windows bug that macOS hides: Unix happily
        # deletes an open file, Windows refuses. So probe live handles directly
        # rather than inferring it from a cleanup that always succeeds here.
        # A *closed* Connection is still a Connection object until collected,
        # so identity is not the test — whether it still answers is.
        gc.collect()
        live = []
        for o in gc.get_objects():
            if isinstance(o, sqlite3.Connection):
                try:
                    o.execute("SELECT 1")
                    live.append(o)
                except sqlite3.ProgrammingError:
                    pass  # already closed, which is the point
        assert not live, f"{len(live)} sqlite connection(s) left open — locks files on Windows"

        print("store self-check ok — two DBs, runs isolated, unicode round-trips, "
              "chain catches a rewrite and a deletion, no leaked handles")
    finally:
        LOGS_DB, RUNS_DB = real
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    use_utf8_stdout()
    if "--runs" in sys.argv:
        for r in list_runs():
            print(f"{r['run_id']}  {r['mode']:<8} {r['model']:<24} "
                  f"{r['events']:>4} events  {r['status']}")
    elif "--verdicts" in sys.argv:
        # the flat view verdicts.jsonl used to give, derived from the chain
        for r in list_runs():
            for e in load_run(r["run_id"])["events"]:
                if e["kind"] in ("verdict", "grade"):
                    print(f"{r['run_id']}  {e['kind']:<8} {_canon(e['payload'])[:150]}")
    else:
        _self_check()
