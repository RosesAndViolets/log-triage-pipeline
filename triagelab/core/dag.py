"""A DAG and the engine that walks it, recording every step onto a chain.

Small on purpose. There is no scheduler, no retry policy, no plugin registry —
those arrive the moment someone needs them and not before. What this buys over
a hardcoded call chain is exactly two things:

  * a declared graph, so a branch (push mode vs agentic mode) is data rather
    than an `if` buried three frames down;
  * uniform recording, so every node's entry, exit, skip and failure lands on
    the evidence chain in the same shape without each node remembering to.

`fn` takes the run context and returns a dict merged back into it. Nodes talk
to each other only through that context, which is what makes them orderable.
"""

from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class Node:
    name: str
    needs: tuple[str, ...] = ()
    fn: Callable[[dict], dict] | None = None
    when: Callable[[dict], bool] | None = None   # None means always
    # What the chain records on exit. Keeping this per-node stops payloads
    # ballooning into "the whole context, serialised" the first time anyone
    # puts something large in there.
    records: tuple[str, ...] = ()


class CycleError(ValueError):
    pass


class _Halt:
    """Returned by a node to end this walk cleanly — not an error.

    A gate that rejects a record is finished, not broken, and saying so in one
    place beats repeating the same `when` condition on every node downstream.
    """
    def __repr__(self):
        return "HALT"


HALT = _Halt()


@dataclass
class DAG:
    nodes: tuple[Node, ...]
    _by_name: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        self._by_name = {}
        for n in self.nodes:
            if n.name in self._by_name:
                raise ValueError(f"duplicate node: {n.name}")
            self._by_name[n.name] = n
        for n in self.nodes:
            for dep in n.needs:
                if dep not in self._by_name:
                    raise ValueError(f"{n.name} needs unknown node: {dep}")

    def order(self) -> list[Node]:
        """Kahn's algorithm. Ties break on declaration order so a run is stable."""
        indeg = {n.name: len(n.needs) for n in self.nodes}
        ready = [n for n in self.nodes if not n.needs]
        out, seen = [], set()
        while ready:
            n = ready.pop(0)
            if n.name in seen:
                continue
            seen.add(n.name)
            out.append(n)
            for m in self.nodes:            # declaration order, not dict order
                if n.name in m.needs:
                    indeg[m.name] -= 1
                    if indeg[m.name] == 0:
                        ready.append(m)
        if len(out) != len(self.nodes):
            stuck = sorted(set(self._by_name) - seen)
            raise CycleError(f"cycle among: {', '.join(stuck)}")
        return out

    def run(self, ctx: dict, chain=None, prefix: str = "",
            disabled: frozenset = frozenset()) -> dict:
        """Walk the graph. Returns the context; the chain gets the narrative.

        A node whose `when` is False is skipped along with nothing else — its
        dependents still run, because "push mode did not fetch context" must
        not look the same as "the run died here". A node that raises stops the
        walk and is recorded as an error; a half-finished run that says so is
        worth more than one that quietly returns.

        `disabled` is the operator switching a node off for this run. It is
        recorded with a different reason from `when`, deliberately: reading a
        chain back, "push mode so investigate never ran" and "somebody turned
        the judge off" must not look identical.
        """
        for node in self.order():
            name = prefix + node.name
            if node.name in disabled or name in disabled:
                if chain:
                    chain.append(name, "skip", {"why": "switched off"})
                continue
            if node.when is not None and not node.when(ctx):
                if chain:
                    chain.append(name, "skip", {"why": "condition not met"})
                continue
            if chain:
                chain.append(name, "enter", _peek(ctx, node.needs))
            try:
                out = node.fn(ctx) if node.fn else {}
            except Exception as e:
                if chain:
                    chain.append(name, "error",
                                 {"type": type(e).__name__, "message": str(e)[:500]})
                raise
            if out is HALT:
                if chain:
                    # Whatever the node put in the context still gets recorded:
                    # "this record was turned away" is only useful with the record.
                    chain.append(name, "exit", {**_peek(ctx, node.records), "halted": True})
                return ctx
            out = out or {}
            ctx.update(out)
            if chain:
                chain.append(name, "exit", _peek(ctx, node.records) if node.records else out)
        return ctx


def _peek(ctx: dict, keys) -> dict:
    """Only the named keys, stringified — payloads stay small and JSON-safe."""
    got = {}
    for k in keys:
        if k in ctx:
            v = ctx[k]
            got[k] = v if isinstance(v, (int, float, bool, type(None))) else str(v)[:400]
    return got


def _self_check():
    from triagelab.core import store

    order = []

    def rec(tag):
        return lambda ctx: (order.append(tag) or {tag: True})

    g = DAG((
        Node("a", fn=rec("a"), records=("a",)),
        Node("b", needs=("a",), fn=rec("b")),
        Node("c", needs=("a",), fn=rec("c"), when=lambda ctx: False),
        Node("d", needs=("b", "c"), fn=rec("d")),
    ))
    names = [n.name for n in g.order()]
    assert names == ["a", "b", "c", "d"], names
    assert names.index("a") < names.index("b") < names.index("d")

    ctx = g.run({})
    # c was skipped, but d still ran: a skipped branch is not a dead run
    assert order == ["a", "b", "d"], order
    assert ctx["a"] and ctx["b"] and ctx["d"] and "c" not in ctx

    # A switched-off node is skipped for a different reason than an unmet
    # condition, and the two must stay distinguishable in the record.
    order.clear()
    ctx2 = g.run({}, disabled=frozenset({"b"}))
    assert order == ["a", "d"], order
    assert "b" not in ctx2 and ctx2["d"]

    # HALT ends the walk without it counting as a failure
    order.clear()
    stop = DAG((Node("one", fn=rec("one")),
                Node("two", needs=("one",), fn=lambda c: HALT),
                Node("three", needs=("two",), fn=rec("three"))))
    stop.run({})
    assert order == ["one"], f"walk continued past HALT: {order}"

    # a cycle is refused, and says which nodes are in it
    try:
        DAG((Node("x", needs=("y",)), Node("y", needs=("x",)))).order()
        raise AssertionError("cycle accepted")
    except CycleError as e:
        assert "x" in str(e) and "y" in str(e), e

    # unknown dependency and duplicate names are refused at construction
    for bad, why in (
        ((Node("p", needs=("nope",)),), "unknown node"),
        ((Node("p"), Node("p")), "duplicate"),
    ):
        try:
            DAG(bad)
            raise AssertionError(f"accepted {why}")
        except ValueError:
            pass

    # the chain gets the narrative, including the skip and the failure
    import shutil
    import tempfile
    from pathlib import Path

    real = (store.LOGS_DB, store.RUNS_DB)
    tmp = Path(tempfile.mkdtemp())
    store.LOGS_DB, store.RUNS_DB = tmp / "l.db", tmp / "r.db"
    try:
        with store.open_run(model="m", mode="test", run_id="t1") as (_, ch):
            g.run({}, ch)
        ev = [(e["node"], e["kind"]) for e in store.load_run("t1")["events"]]
        assert ("c", "skip") in ev, ev

        # the two skip reasons must be told apart in the chain
        with store.open_run(model="m", mode="test", run_id="t3") as (_, ch3):
            g.run({}, ch3, disabled=frozenset({"b"}))
        why = {e["node"]: e["payload"].get("why")
               for e in store.load_run("t3")["events"] if e["kind"] == "skip"}
        assert why["b"] == "switched off", why
        assert why["c"] == "condition not met", why
        assert ev.count(("a", "enter")) == 1 and ev.count(("a", "exit")) == 1
        assert store.verify("t1") is None

        boom = DAG((Node("ok", fn=rec("ok")),
                    Node("bad", needs=("ok",), fn=lambda c: (_ for _ in ()).throw(RuntimeError("nope"))),
                    Node("after", needs=("bad",), fn=rec("after"))))
        try:
            with store.open_run(model="m", mode="test", run_id="t2") as (_, ch2):
                boom.run({}, ch2)
            raise AssertionError("failure did not propagate")
        except RuntimeError:
            pass
        ev2 = [(e["node"], e["kind"]) for e in store.load_run("t2")["events"]]
        assert ("bad", "error") in ev2, ev2
        assert not any(n == "after" for n, _ in ev2), "walk continued past a failure"
        assert store.load_run("t2")["run"]["status"].startswith("failed")
    finally:
        store.LOGS_DB, store.RUNS_DB = real
        shutil.rmtree(tmp, ignore_errors=True)

    print("dag self-check ok — order, cycle refused, skip continues, failure halts "
          "and is recorded")


if __name__ == "__main__":
    from triagelab.core import store

    store.use_utf8_stdout()
    _self_check()
