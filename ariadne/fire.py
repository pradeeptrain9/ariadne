"""Send an Alertmanager-shaped notification to the demo server, as Alertmanager would.

    python -m ariadne.fire --service catalog-service            # POST it, print the result
    python -m ariadne.fire --service catalog-service --print    # print the JSON only

`--print` emits exactly the body Alertmanager's webhook receiver sends, so it can be piped into
`curl` to show the request is nothing special:

    python -m ariadne.fire --print | curl -s -H 'Content-Type: application/json' \\
        --data @- http://127.0.0.1:8000/api/alertmanager

The one thing this does that a real Alertmanager does not: it places `startsAt` on the seeded
graph's clock — a few minutes after the most recent deploy that touched the service — because
the graph is synthetic and its deploys are not on today's date. A wall-clock `startsAt` is
accepted too; against this graph its window simply holds no deploys.
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.request
from typing import Any

from .graph import Ariadne
from .ingest import fingerprint_of, format_rfc3339

#: How long after the deploy the alert starts — inside the window the signature looks at.
DEFAULT_LAG_MIN = 12

_LATEST_DEPLOY = """
MATCH (d:Deploy)-[t:TOUCHED]->(s:Service {name: $service})
WHERE ID(t) >= 0
RETURN d.id AS id, d.at AS at ORDER BY d.at DESC LIMIT 1
"""


def starts_at_on_graph_clock(a: Ariadne, service: str, lag_min: int = DEFAULT_LAG_MIN) -> int:
    row = a.one(_LATEST_DEPLOY, {"service": service})
    if row is None:
        raise SystemExit(f"no deploy has touched {service!r}; pass --starts-at explicitly")
    return int(row["at"]) + lag_min * 60_000


def build_payload(*, service: str, metric: str, severity: str, value: float,
                  starts_at_ms: int, alertname: str | None = None) -> dict[str, Any]:
    """An Alertmanager webhook notification (version 4) holding one firing alert."""
    alertname = alertname or {"latency_p99": "HighLatencyP99", "error_rate": "HighErrorRate",
                              "saturation": "Saturation", "queue_depth": "QueueBacklog"
                              }.get(metric, "AriadneDemo")
    labels = {"alertname": alertname, "service": service, "metric": metric,
              "severity": severity}
    return {
        "version": "4",
        "groupKey": f'{{}}:{{alertname="{alertname}", service="{service}"}}',
        "truncatedAlerts": 0,
        "status": "firing",
        "receiver": "ariadne",
        "groupLabels": {"alertname": alertname, "service": service},
        "commonLabels": labels,
        "commonAnnotations": {},
        "externalURL": "http://alertmanager.local:9093",
        "alerts": [{
            "status": "firing",
            "labels": labels,
            "annotations": {"summary": f"{metric} on {service}", "value": f"{value}"},
            "startsAt": format_rfc3339(starts_at_ms),
            "endsAt": "0001-01-01T00:00:00Z",
            "generatorURL": "http://prometheus.local:9090/graph",
            "fingerprint": fingerprint_of(labels),
        }],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Fire an Alertmanager webhook at the demo server.")
    ap.add_argument("--service", default="catalog-service")
    ap.add_argument("--metric", default="latency_p99")
    ap.add_argument("--severity", default="high")
    ap.add_argument("--value", type=float, default=18.4)
    ap.add_argument("--starts-at", type=int, help="epoch ms; default: on the graph's clock")
    ap.add_argument("--lag-min", type=int, default=DEFAULT_LAG_MIN,
                    help="minutes after the service's latest deploy (default %(default)s)")
    ap.add_argument("--url", default="http://127.0.0.1:8000/api/alertmanager")
    ap.add_argument("--print", action="store_true", help="print the payload instead of sending")
    args = ap.parse_args()

    if args.starts_at is None:
        with Ariadne() as a:
            starts_at = starts_at_on_graph_clock(a, args.service, args.lag_min)
    else:
        starts_at = args.starts_at

    payload = build_payload(service=args.service, metric=args.metric, severity=args.severity,
                            value=args.value, starts_at_ms=starts_at)
    if args.print:
        print(json.dumps(payload, indent=2))
        return

    req = urllib.request.Request(args.url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        import certifi  # present on most machines; python.org's macOS build ships no CA bundle
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        context = None
    try:
        with urllib.request.urlopen(req, timeout=60, context=context) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        sys.exit(f"{exc.code}: {exc.read().decode()}")
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            sys.exit("this Python has no CA certificates to verify HTTPS with. Either run "
                     "macOS's 'Install Certificates.command' for this Python, `pip install "
                     "certifi`, or send it with curl:  python -m ariadne.fire --print | curl -s "
                     f"-H 'Content-Type: application/json' --data @- {args.url}")
        sys.exit(f"could not reach {args.url}: {exc.reason} — is `python -m ariadne.server` up?")

    for a in result["accepted"]:
        print(f"accepted  {a['id']}  {a['service']} · {a['metric']} · {a['severity']}  "
              f"signature {a['signature']}{'  (re-send, already in the graph)' if a['duplicate'] else ''}")
    for s in result["skipped"]:
        print(f"skipped   alert {s['index']}: {s['reason']}")


if __name__ == "__main__":
    main()
