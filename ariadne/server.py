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
from typing import Any
from urllib.parse import urlparse, parse_qs

from . import ledger as L
from .agent import investigate
from .graph import Ariadne

WEB = Path(__file__).resolve().parent.parent / "web"

#: The runs committed to the repo, used when this machine has not measured anything yet.
SHIPPED_LEDGER = Path(__file__).resolve().parent.parent / "docs" / "measurements" / "runs.jsonl"


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


def alert_payload(a: Ariadne, alert_id: str) -> dict[str, Any] | None:
    return a.one("""
        MATCH (x:Alert {id: $id})-[:FIRED_ON]->(s:Service)
        RETURN x.id AS id, x.at AS at, x.metric AS metric, x.value AS value,
               x.severity AS severity, s.name AS service
    """, {"id": alert_id})


def demo_alerts(a: Ariadne) -> dict[str, Any]:
    """Scenario alerts, plus a few live ones, for the picker."""
    path = Path("fixtures/scenarios.json")
    scenarios = json.loads(path.read_text()) if path.exists() else {}
    recent = a.q("""
        MATCH (x:Alert)-[:FIRED_ON]->(s:Service)
        WHERE NOT (:Incident)-[:TRIGGERED_BY]->(x)
        RETURN x.id AS id, x.at AS at, x.metric AS metric, x.value AS value,
               x.severity AS severity, s.name AS service
        ORDER BY x.at DESC LIMIT 12
    """)
    return {"scenarios": scenarios, "unresolved": recent}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ariadne"

    def __init__(self, *args: Any, graph: Ariadne, **kwargs: Any) -> None:
        self._graph = graph
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
            # threads, and the server handles requests on a thread pool.
            with Ariadne() as own:
                try:
                    investigate(own, alert, force_mode=mode,
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


def serve(port: int = 8000) -> None:
    graph = Ariadne()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, graph=graph))
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
