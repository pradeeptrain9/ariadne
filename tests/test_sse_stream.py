"""The live-run path, driven end to end without an API key.

`/api/investigate` is the demo: the tool calls appear as they happen, the path lights up, the
cost counter moves. It was also the one path with no test, because the only thing on it that
needs a credential — the model's HTTP call — sat in the middle of everything else, so a missing
key left the SSE framing, the event order, the tool execution, the graph write-back and the
ledger row all unverified together.

The client is scripted here instead. Everything else is real: a real socket, the real handler,
the real tool bodies against the real seeded graph, the real `Run` accounting, and the real
write-back through `record_outcome`. What these tests do *not* cover is the one call they
replace — whether the model, given this system prompt and these tools, chooses this sequence.
That needs a key and a live run.

The scripted runner yields a turn and executes that turn's tools *afterwards*, which is what the
SDK's `tool_runner` does. The order matters: a stub that ran the tools before yielding would
make the off-by-one this stream once had — row counts and latencies reported against the
previous call's name — pass as correct.
"""

from __future__ import annotations

import http.client
import json
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from typing import Any

import pytest

from ariadne import tools as T
from ariadne.graph import Ariadne
from ariadne.server import Handler
from ariadne.signature import signature_of


# --- the scripted client -------------------------------------------------------------------


class _Usage:
    """Just the four fields `Run.add_usage` reads."""

    def __init__(self, input_tokens: int = 1_000, output_tokens: int = 200,
                 cache_write: int = 0, cache_read: int = 0) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_creation_input_tokens = cache_write
        self.cache_read_input_tokens = cache_read


class _Text:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _ToolUse:
    type = "tool_use"

    def __init__(self, name: str, args: dict[str, Any]) -> None:
        self.name = name
        self.input = args


class _Message:
    def __init__(self, content: list[Any], usage: _Usage) -> None:
        self.content = content
        self.usage = usage


class ScriptedRunner:
    """Replays scripted turns, running each turn's tools after the turn is yielded."""

    def __init__(self, turns: list[tuple[list[Any], _Usage]], raise_at: int | None = None) -> None:
        self._turns = turns
        self._raise_at = raise_at

    def __iter__(self):
        for i, (content, usage) in enumerate(self._turns):
            if self._raise_at == i:
                raise RuntimeError("scripted model failure")
            yield _Message(content, usage)
            for block in content:
                if block.type == "tool_use":
                    # The same wrapper the real transport calls, so the result still has to be
                    # JSON-serializable to pass here.
                    T._as_text(T.TOOL_FUNCS[block.name])(**block.input)


class ScriptedClient:
    """Enough of `anthropic.Anthropic` for `investigate`: `.beta.messages.tool_runner`."""

    def __init__(self, turns: list[tuple[list[Any], _Usage]], raise_at: int | None = None) -> None:
        self._turns = turns
        self._raise_at = raise_at
        self.kwargs: dict[str, Any] = {}

    @property
    def beta(self) -> "ScriptedClient":
        return self

    @property
    def messages(self) -> "ScriptedClient":
        return self

    def tool_runner(self, **kwargs: Any) -> ScriptedRunner:
        self.kwargs = kwargs
        return ScriptedRunner(self._turns, self._raise_at)


# --- harness -------------------------------------------------------------------------------


def _serve(graph: Ariadne, client: ScriptedClient, ledger):
    """A real HTTP server on an ephemeral port, handing out the scripted client."""
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        partial(Handler, graph=graph, client_factory=lambda: client, ledger=ledger))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _stream(httpd, path: str) -> tuple[int, list[tuple[str, Any]]]:
    """GET an SSE endpoint and parse it into (event, data) pairs."""
    conn = http.client.HTTPConnection(*httpd.server_address, timeout=60)
    conn.request("GET", path)
    resp = conn.getresponse()
    if resp.status != 200:
        body = json.loads(resp.read().decode())
        conn.close()
        return resp.status, [("http-error", body)]

    events: list[tuple[str, Any]] = []
    kind: str | None = None
    for raw in resp.read().decode().splitlines():
        if raw.startswith("event: "):
            kind = raw[len("event: "):]
        elif raw.startswith("data: "):
            events.append((kind or "?", json.loads(raw[len("data: "):])))
    conn.close()
    return resp.status, events


@pytest.fixture
def unresolved(seeded):
    """An alert with no incident yet, so the scripted run writes a fresh one we can delete."""
    row = seeded.one("""
        MATCH (x:Alert)-[fo:FIRED_ON]->(s:Service)
        WHERE ID(fo) >= 0 AND NOT (:Incident)-[:TRIGGERED_BY]->(x)
        RETURN x.id AS id, x.at AS at, x.metric AS metric, x.value AS value,
               x.severity AS severity, s.name AS service
        ORDER BY x.id LIMIT 1
    """)
    assert row is not None, "seed has no unresolved alert to investigate"
    return row


@pytest.fixture
def script(seeded, unresolved):
    """A cold investigation's tool sequence, with arguments taken from the graph."""
    alert = unresolved
    service, iid = alert["service"], f"inc-{alert['id']}"
    sig, _shape = signature_of(seeded, alert)

    calls = [
        ("upstream_candidates", {"service": service, "hops": 3}),
        ("deploys_in_window", {"services": [service], "alert_at": alert["at"]}),
        ("blast_radius", {"service": service}),
        ("owner_of", {"service": service}),
        ("record_outcome", {
            "incident_id": iid, "alert_id": alert["id"], "signature": sig,
            "culprit_sha": None, "confidence": 0.5, "impacted_services": [],
        }),
    ]
    turns = [
        ([_Text("Narrowing the dependency cone."), _ToolUse(*calls[0])],
         _Usage(input_tokens=8_000, output_tokens=150)),
        ([_ToolUse(*calls[1])], _Usage(input_tokens=400, output_tokens=120, cache_read=7_900)),
        # Two tools in one turn: the stream has to report both, in order.
        ([_ToolUse(*calls[2]), _ToolUse(*calls[3])],
         _Usage(input_tokens=500, output_tokens=140, cache_read=7_900)),
        ([_Text("- no commit shipped in the window; the cause is structural"),
          _ToolUse(*calls[4])],
         _Usage(input_tokens=600, output_tokens=200, cache_read=7_900)),
        # A closer after record_outcome: the verdict is in an earlier block, and keeping only
        # the last one would throw it away.
        ([_Text("Paged the owner.")], _Usage(input_tokens=300, output_tokens=40,
                                            cache_read=7_900)),
    ]
    yield alert, sig, calls, turns

    seeded.write("MATCH (i:Incident {id: $i}) DETACH DELETE i", {"i": iid})
    seeded.write("MATCH (p:Plan {signature: $s}) DETACH DELETE p", {"s": sig})
    seeded.write("MATCH (s:Step) WHERE s.id STARTS WITH $i DETACH DELETE s", {"i": iid})


# --- the stream ----------------------------------------------------------------------------


def test_stream_reports_every_tool_call_in_order_with_its_own_numbers(
        seeded, script, tmp_path):
    """The off-by-one guard: each `tool` event carries the row count and latency of the call it
    names, not of the one before it."""
    alert, _sig, calls, turns = script
    client = ScriptedClient(turns)
    httpd = _serve(seeded, client, tmp_path / "runs.jsonl")
    try:
        status, events = _stream(httpd, f"/api/investigate?alert={alert['id']}&mode=cold")
    finally:
        httpd.shutdown()

    assert status == 200
    tools = [(e["name"], e["rows"], e["ms"], e["ok"]) for k, e in events if k == "tool"]
    assert [t[0] for t in tools] == [name for name, _ in calls]
    assert all(ok for *_, ok in tools)

    # Row counts are the real tools' real counts, not the stub's invention.
    with T.bind(seeded):
        expected = []
        for name, args in calls[:-1]:  # record_outcome writes; do not run it twice
            out = T.TOOL_FUNCS[name](**args)
            expected.append(len(out["rows"]) if isinstance(out, dict) and "rows" in out else 1)
    assert [rows for _, rows, _, _ in tools][:-1] == expected
    assert any(rows > 0 for _, rows, _, _ in tools), "every tool returned nothing"


def test_stream_frames_and_orders_the_whole_run(seeded, script, tmp_path):
    alert, _sig, calls, turns = script
    httpd = _serve(seeded, ScriptedClient(turns), tmp_path / "runs.jsonl")
    try:
        status, events = _stream(httpd, f"/api/investigate?alert={alert['id']}&mode=cold")
    finally:
        httpd.shutdown()

    kinds = [k for k, _ in events]
    assert status == 200
    assert kinds[0] == "start"
    assert kinds[-1] == "end", "the stream must close with an end event"
    assert kinds.count("done") == 1
    assert kinds.index("done") == len(kinds) - 2
    assert kinds.count("usage") == len(turns)
    assert kinds.count("tool") == len(calls)
    # A tool cannot be reported before the run has started or after it is done.
    assert kinds.index("start") < kinds.index("tool") < kinds.index("done")

    start = next(e for k, e in events if k == "start")
    assert start["alert"]["id"] == alert["id"]
    assert start["mode"] == "cold" and start["effort"] == "high"
    assert start["signature"] and start["model"]

    done = next(e for k, e in events if k == "done")
    assert done["run"]["tool_calls"] == len(calls)
    assert done["run"]["turns"] == len(turns)
    assert done["run"]["cost_usd"] > 0
    # Both text blocks survive, including the one written before `record_outcome`.
    assert "structural" in done["text"] and "Paged the owner." in done["text"]


def test_usage_accumulates_across_turns(seeded, script, tmp_path):
    """The cost counter on the page moves because each turn's usage is added, not replaced."""
    alert, _sig, _calls, turns = script
    httpd = _serve(seeded, ScriptedClient(turns), tmp_path / "runs.jsonl")
    try:
        _status, events = _stream(httpd, f"/api/investigate?alert={alert['id']}&mode=cold")
    finally:
        httpd.shutdown()

    usages = [e for k, e in events if k == "usage"]
    assert [u["turns"] for u in usages] == list(range(1, len(turns) + 1))
    assert usages == sorted(usages, key=lambda u: u["tokens"]), "tokens must only grow"
    assert usages[-1]["cache_read"] == sum(
        u.cache_read_input_tokens for _, u in turns)


def test_the_run_reaches_the_graph_and_the_ledger(seeded, script, tmp_path):
    """The stream is not a performance: the tools really wrote, and the row really landed."""
    alert, sig, calls, turns = script
    ledger = tmp_path / "runs.jsonl"
    httpd = _serve(seeded, ScriptedClient(turns), ledger)
    try:
        _status, events = _stream(httpd, f"/api/investigate?alert={alert['id']}&mode=cold")
    finally:
        httpd.shutdown()

    incident = seeded.one("""MATCH (i:Incident {id: $i})-[tb:TRIGGERED_BY]->(x:Alert)
                             WHERE ID(tb) >= 0 RETURN i.id AS id, x.id AS alert""",
                          {"i": f"inc-{alert['id']}"})
    assert incident is not None and incident["alert"] == alert["id"]
    assert seeded.scalar("MATCH (p:Plan {signature: $s}) RETURN count(p)", {"s": sig}) == 1

    rows = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["alert_id"] == alert["id"]
    assert rows[0]["mode"] == "cold"
    assert rows[0]["tool_calls"] == len(calls)
    assert [c["tool"] for c in rows[0]["calls"]] == [n for n, _ in calls]

    done = next(e for k, e in events if k == "done")
    assert done["run"]["impacted"] == rows[0]["impacted"]


def test_the_request_carries_the_cache_breakpoint_and_the_mode_s_effort(
        seeded, script, tmp_path):
    """The cost claim depends on the prefix being cacheable and on warm runs costing less
    effort. Both are decided here, in the request the stream builds."""
    alert, _sig, _calls, turns = script
    client = ScriptedClient(turns)
    httpd = _serve(seeded, client, tmp_path / "runs.jsonl")
    try:
        _stream(httpd, f"/api/investigate?alert={alert['id']}&mode=warm")
    finally:
        httpd.shutdown()

    kw = client.kwargs
    assert kw["output_config"] == {"effort": "low"}, "a warm run must not buy high effort"
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kw["tools"] is T.TOOLS
    assert kw["messages"][0]["role"] == "user"
    assert "memory" in kw["messages"][0]["content"].lower()


# --- the unhappy paths ---------------------------------------------------------------------


def test_a_failing_model_call_ends_the_stream_with_an_error(seeded, script, tmp_path):
    """A demo that hangs is worse than one that says what broke."""
    alert, _sig, _calls, turns = script
    httpd = _serve(seeded, ScriptedClient(turns, raise_at=2), tmp_path / "runs.jsonl")
    try:
        status, events = _stream(httpd, f"/api/investigate?alert={alert['id']}&mode=cold")
    finally:
        httpd.shutdown()

    kinds = [k for k, _ in events]
    assert status == 200  # the headers were already sent when the failure happened
    assert "error" in kinds and kinds[-1] == "end"
    err = next(e for k, e in events if k == "error")
    assert "scripted model failure" in err["message"]


def test_an_unknown_alert_is_a_404_not_an_empty_stream(seeded, script, tmp_path):
    _alert, _sig, _calls, turns = script
    httpd = _serve(seeded, ScriptedClient(turns), tmp_path / "runs.jsonl")
    try:
        status, events = _stream(httpd, "/api/investigate?alert=alr-does-not-exist")
    finally:
        httpd.shutdown()

    assert status == 404
    assert "no such alert" in events[0][1]["error"]


def test_the_stream_investigates_the_graph_the_server_was_given(seeded, script, tmp_path):
    """The worker opens its own connection, which must still be the page's graph. A bare
    `Ariadne()` would take `ARIADNE_GRAPH` and investigate a different one."""
    alert, _sig, _calls, turns = script
    assert seeded.name != "ariadne", "fixture graph must differ from the module default"
    httpd = _serve(seeded, ScriptedClient(turns), tmp_path / "runs.jsonl")
    try:
        _status, events = _stream(httpd, f"/api/investigate?alert={alert['id']}&mode=cold")
    finally:
        httpd.shutdown()

    assert [k for k, _ in events][-1] == "end"
    # The write-back landed in the test graph, so that is the graph the agent traversed.
    assert seeded.scalar("MATCH (i:Incident {id: $i}) RETURN count(i)",
                         {"i": f"inc-{alert['id']}"}) == 1
