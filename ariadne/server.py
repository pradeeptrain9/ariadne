"""Demo server: the graph, the live investigation, and the ledger.

Standard library only. The demo has to run on a judge's machine from a clone and a `docker
compose up`, so it does not get to depend on a web framework.

Investigations stream over Server-Sent Events, which is the whole point of the page: the tool
calls appear as they happen, the path lights up as the graph is traversed, and the cost counter
moves. A page that showed only the final verdict would hide the part that is interesting.
"""

from __future__ import annotations

import argparse
import json
import queue
import threading
import traceback
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse, parse_qs

from . import ledger as L
from . import tools as T
from .agent import investigate
from .graph import Ariadne
from .ingest import PayloadError, ingest
from .scoring import explain

WEB = Path(__file__).resolve().parent.parent / "web"

#: An Alertmanager notification is a few kilobytes; anything near this is not one.
MAX_BODY = 1_000_000

#: The runs committed to the repo, used when this machine has not measured anything yet.
SHIPPED_LEDGER = L.MEASURED_LEDGER


def ledger_rows() -> tuple[Path, list[dict[str, Any]]]:
    """This session's ledger if it holds anything, else the measurements the repo ships.

    A fresh clone has no `runs.jsonl`, so the cold-vs-warm panel opened empty until the judge had
    both an API key and two completed arms. The payload names the file it read, and the page says
    so: pre-recorded numbers shown as if they were from this machine would be a lie, and the
    whole point of the ledger is that no number is asserted without a file behind it.
    """
    rows = L.load()
    if rows:
        return L.LEDGER_PATH, rows
    return SHIPPED_LEDGER, L.load(SHIPPED_LEDGER)


def graph_payload(a: Ariadne) -> dict[str, Any]:
    """The mesh, for drawing. Small enough to send whole."""
    services = a.q("""
        MATCH (s:Service)
        OPTIONAL MATCH (p:Person)-[o:OWNS {rotation: 'primary'}]->(s)
        RETURN s.name AS name, s.tier AS tier, s.betweenness AS betweenness,
               p.handle AS owner
        ORDER BY s.tier, s.name
    """)
    deps = a.q("""
        MATCH (x:Service)-[d:DEPENDS_ON]->(y:Service)
        WHERE ID(d) >= 0
        RETURN x.name AS source, y.name AS target, d.critical AS critical,
               d.calls_per_min AS calls
    """)
    stats = {
        label.lower(): a.scalar(f"MATCH (n:{label}) RETURN count(n) AS c")
        for label in ("Service", "Commit", "Deploy", "Alert", "Incident", "Plan", "Step")
    }
    return {"services": services, "deps": deps, "stats": stats}


#: The chain behind a recorded verdict: alert → failing service → deploy → commit → author.
#: This is the one query here that the tool layer does not already have; everything else the
#: verdict needs is fetched by calling the tools, so there is no second copy of that Cypher.
_VERDICT_CHAIN = """
MATCH (i:Incident {id: $iid})-[tb:TRIGGERED_BY]->(x:Alert)-[fo:FIRED_ON]->(f:Service)
WHERE ID(tb) >= 0 AND ID(fo) >= 0
OPTIONAL MATCH (i)-[cb:CAUSED_BY]->(c:Commit)
OPTIONAL MATCH (c)-[ci:IN]->(d:Deploy)
OPTIONAL MATCH (c)-[ab:AUTHORED_BY]->(p:Person)
RETURN i.status AS status, f.name AS failing, x.id AS alert, x.metric AS metric,
       x.value AS value, x.severity AS severity, x.at AS alert_at,
       c.sha AS sha, c.msg AS msg, cb.confidence AS confidence,
       cb.model_confidence AS model_confidence,
       d.id AS deploy, d.at AS deploy_at, p.handle AS author, p.name AS author_name
"""


def verdict_payload(a: Ariadne, incident_id: str) -> dict[str, Any] | None:
    """The verdict as the graph holds it, not as the model wrote it.

    The page renders this above the model's prose. Both describe the same incident, and when they
    disagree the computed one is the answer — the culprit, the confidence and its terms, the
    impact and the owner are all traversals and arithmetic, and `record_outcome` is what the
    model actually asserted. Showing only the prose would make the demo a story about an answer
    instead of the answer.
    """
    chain = a.one(_VERDICT_CHAIN, {"iid": incident_id})
    if chain is None:
        return None

    failing = chain["failing"]
    with T.bind(a):  # no Run: these lookups are the page's, not steps of an investigation
        impact = T.blast_radius(failing)
        owners = T.owner_of(failing)["rows"]
        runbooks = T.runbook_for(failing)["rows"]
        cone = T.upstream_candidates(failing, 3)["rows"]
        suspects = (T.suspect_commits([chain["deploy"]], failing)["rows"]
                    if chain["deploy"] else [])

    culprit: dict[str, Any] | None = None
    if chain["sha"]:
        row = next((r for r in suspects if r["sha"] == chain["sha"]), None)
        culprit = {
            "sha": chain["sha"],
            "msg": chain["msg"],
            "author": chain["author"],
            "author_name": chain["author_name"],
            "deploy": chain["deploy"],
            "deploy_at": chain["deploy_at"],
            "confidence": chain["confidence"],
            "model_confidence": chain["model_confidence"],
            "overlap": row["overlap"] if row else None,
            "files_changed": row["files_changed"] if row else None,
        }
        # The terms, recomputed from the graph rather than parsed back out of the model's
        # sentence. `confidence_facts` supplies the real dependency distance; assuming 0 would
        # overstate a culprit that shipped several hops upstream.
        facts = T.confidence_facts(a, alert_id=chain["alert"], sha=chain["sha"])
        if facts is not None:
            culprit["terms"] = explain(
                overlap=facts["overlap"], files_changed=facts["files_changed"],
                distance=facts["distance"], deploy_at=facts["deploy_at"],
                alert_at=facts["alert_at"],
            )
            culprit["distance_hops"] = facts["distance"]

    return {
        "incident": incident_id,
        "status": chain["status"],
        "alert": {"id": chain["alert"], "metric": chain["metric"], "value": chain["value"],
                  "severity": chain["severity"], "at": chain["alert_at"]},
        "failing": failing,
        "culprit": culprit,
        "impacted": impact["impacted"],
        "impacted_count": impact["impacted_count"],
        "tier1_impacted": impact["tier1_impacted"],
        "cone": [r["service"] for r in cone],
        "owner": owners[0] if owners else None,
        "owners": owners,
        "runbook": runbooks[0] if runbooks else None,
    }


def alert_payload(a: Ariadne, alert_id: str) -> dict[str, Any] | None:
    return a.one("""
        MATCH (x:Alert {id: $id})-[:FIRED_ON]->(s:Service)
        RETURN x.id AS id, x.at AS at, x.metric AS metric, x.value AS value,
               x.severity AS severity, s.name AS service
    """, {"id": alert_id})


def demo_alerts(a: Ariadne) -> dict[str, Any]:
    """Scenario alerts, alerts pushed by the Alertmanager webhook, and a few seeded unresolved
    ones, for the picker. Pushed alerts are listed by when they arrived, not by `at`: the page
    polls this to notice a new one, and a pushed alert can carry any `startsAt`."""
    path = Path("fixtures/scenarios.json")
    scenarios = json.loads(path.read_text()) if path.exists() else {}
    pushed = a.q("""
        MATCH (x:Alert {source: 'alertmanager'})-[:FIRED_ON]->(s:Service)
        RETURN x.id AS id, x.at AS at, x.metric AS metric, x.value AS value,
               x.severity AS severity, s.name AS service, x.received_at AS received_at,
               x.alertname AS alertname
        ORDER BY x.received_at DESC LIMIT 5
    """)
    recent = a.q("""
        MATCH (x:Alert)-[:FIRED_ON]->(s:Service)
        WHERE NOT (:Incident)-[:TRIGGERED_BY]->(x) AND x.source IS NULL
        RETURN x.id AS id, x.at AS at, x.metric AS metric, x.value AS value,
               x.severity AS severity, s.name AS service
        ORDER BY x.at DESC LIMIT 12
    """)
    return {"scenarios": scenarios, "pushed": pushed, "unresolved": recent}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ariadne"

    def __init__(self, *args: Any, graph: Ariadne,
                 client_factory: Callable[[], Any] | None = None,
                 ledger: Path | None = L.LEDGER_PATH, **kwargs: Any) -> None:
        self._graph = graph
        #: Builds the model client for an investigation. `None` lets `investigate` construct the
        #: real one, which is what the demo does. The tests pass a scripted client so the whole
        #: SSE path — framing, event order, tool execution, graph write-back — can be exercised
        #: without an API key; it is the only part of the stream that needs a credential, and
        #: leaving it uninjectable meant leaving the rest of the path unverified.
        self._client_factory = client_factory
        #: Where a streamed run is recorded. The demo appends to the repo-root ledger so the
        #: page's cold-vs-warm panel fills in as runs happen; the tests redirect it so a test
        #: run never lands in a file the submission quotes numbers from.
        self._ledger = ledger
        super().__init__(*args, **kwargs)

    # --- plumbing ------------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # the interesting log is the agent's, not the HTTP one

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(json.dumps(payload, default=str).encode(), "application/json", status)

    def _file(self, name: str) -> None:
        path = (WEB / name).resolve()
        if not path.is_file() or WEB not in path.parents:
            self._json({"error": "not found"}, 404)
            return
        kind = {".html": "text/html; charset=utf-8", ".css": "text/css",
                ".js": "application/javascript"}.get(path.suffix, "application/octet-stream")
        self._send(path.read_bytes(), kind)

    # --- routes --------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path)
        query = parse_qs(route.query)
        try:
            if route.path in ("/", "/index.html"):
                self._file("index.html")
            elif route.path == "/api/graph":
                self._json(graph_payload(self._graph))
            elif route.path == "/api/alerts":
                self._json(demo_alerts(self._graph))
            elif route.path == "/api/ledger":
                source, rows = ledger_rows()
                self._json({"runs": rows, "summary": L.compare(rows),
                            "source": source.name if source == L.LEDGER_PATH
                                      else "docs/measurements/runs.jsonl",
                            "live": source == L.LEDGER_PATH})
            elif route.path == "/api/verdict":
                payload = verdict_payload(self._graph, query.get("incident", [""])[0])
                if payload is None:
                    self._json({"error": "no such incident"}, 404)
                else:
                    self._json(payload)
            elif route.path == "/api/investigate":
                self._stream(query.get("alert", [""])[0], query.get("mode", [None])[0])
            else:
                self._json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as exc:  # a demo that dies silently is worse than one that explains
            traceback.print_exc()
            try:
                self._json({"error": str(exc)}, 500)
            except Exception:
                pass

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path)
        try:
            if route.path != "/api/alertmanager":
                self._json({"error": "not found"}, 404)
                return
            # Every refusal below answers before the body is read, so the unread bytes would be
            # parsed as the next request on a kept-alive socket. Close it instead.
            self.close_connection = True
            length = self.headers.get("Content-Length")
            if length is None:
                self._json({"error": "Content-Length required"}, 411)
                return
            try:
                size = int(length)
            except ValueError:
                self._json({"error": "Content-Length is not a number"}, 400)
                return
            if size < 0 or size > MAX_BODY:
                self._json({"error": f"body over {MAX_BODY} bytes"}, 413)
                return
            self.close_connection = False  # the body is about to be read in full
            try:
                payload = json.loads(self.rfile.read(size).decode() or "null")
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._json({"error": f"body is not JSON: {exc}"}, 400)
                return
            # Its own handle, as the investigation worker has: this route writes, and a
            # FalkorDB connection is not safe to share across the server's request threads.
            with Ariadne(self._graph.name) as own:
                try:
                    result = ingest(own, payload)
                except PayloadError as exc:
                    self._json({"error": str(exc)}, 400)
                    return
            if result["accepted"]:
                print("alertmanager → " + ", ".join(
                    f"{x['id']} on {x['service']}" for x in result["accepted"]))
            self._json(result)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            traceback.print_exc()
            try:
                self._json({"error": str(exc)}, 500)
            except Exception:
                pass

    # --- SSE -----------------------------------------------------------------

    def _stream(self, alert_id: str, mode: str | None) -> None:
        alert = alert_payload(self._graph, alert_id)
        if alert is None:
            self._json({"error": f"no such alert: {alert_id}"}, 404)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        events: queue.Queue[tuple[str, Any] | None] = queue.Queue()

        def worker() -> None:
            # The agent's own graph handle: a FalkorDB connection is not safe to share across
            # threads, and the server handles requests on a thread pool. It must open the graph
            # the server was given by *name* — a bare `Ariadne()` takes the module default from
            # `ARIADNE_GRAPH`, so a server started on any other graph investigated one graph
            # while every other route on the page read another.
            with Ariadne(self._graph.name) as own:
                try:
                    investigate(own, alert, force_mode=mode,
                                client=(self._client_factory()
                                        if self._client_factory is not None else None),
                                ledger=self._ledger,
                                on_event=lambda k, p: events.put((k, p)), verbose=True)
                except Exception as exc:
                    traceback.print_exc()
                    events.put(("error", {"message": str(exc)}))
                finally:
                    events.put(None)

        threading.Thread(target=worker, daemon=True).start()

        while True:
            item = events.get()
            if item is None:
                break
            kind, payload = item
            try:
                self.wfile.write(
                    f"event: {kind}\ndata: {json.dumps(payload, default=str)}\n\n".encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return  # the page navigated away mid-run; the worker finishes regardless
        try:
            self.wfile.write(b"event: end\ndata: {}\n\n")
            self.wfile.flush()
        except Exception:
            pass


def serve(port: int = 8000, *, client_factory: Callable[[], Any] | None = None,
          ledger: Path | None = L.LEDGER_PATH) -> None:
    graph = Ariadne()
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", port),
        partial(Handler, graph=graph, client_factory=client_factory, ledger=ledger))
    print(f"ariadne demo on http://127.0.0.1:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        graph.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the demo server.")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    serve(args.port)


if __name__ == "__main__":
    main()
