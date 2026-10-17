"""Alert ingestion from a Prometheus Alertmanager webhook.

This is the first real entry point into the graph: Alertmanager's `webhook_config` receiver
POSTs its notification here, and every firing alert in it becomes an `Alert` node with a
`FIRED_ON` edge to the service its labels name. Nothing else is written — the investigation is
still started by a person (or the demo page), because each one is a paid model call and an
endpoint that spends money on every page would be the wrong default.

The payload is Alertmanager's documented webhook format, version 4:
https://prometheus.io/docs/alerting/latest/configuration/#webhook_config

Mapping, and the choices in it:

* **service** — the first of the `service`, `app` or `job` labels. It must name a `Service`
  already in the graph; an alert on a service the graph does not know is reported back as
  skipped rather than creating an orphan node no traversal can reach.
* **metric** — the `metric` label, else `alertname`. The signature maps anything outside its
  controlled vocabulary to `other`, so an unfamiliar name costs memory hits, not correctness.
* **severity** — mapped onto the graph's four levels. A label the map does not know becomes
  `medium` and the response says so: a receiver that drops a page because of a label spelling
  is worse than one that files it at the middle level.
* **at** — `startsAt`, exactly. The demo's graph is synthetic and lives on its own clock, so
  `python -m ariadne.fire` places `startsAt` on that clock; a real Alertmanager sends wall-clock
  time, and against the synthetic graph that window holds no deploys. That is the honest result,
  not a bug to paper over here.
* **id** — derived from Alertmanager's `fingerprint` (stable per label set) and `startsAt`.
  Alertmanager re-sends an unresolved alert every `repeat_interval`; the re-send lands on the same
  id and is acknowledged, not duplicated.

Resolved alerts are skipped: closing an incident is `record_outcome`'s job, and an alert going
quiet is not evidence of what caused it.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from typing import Any

from .graph import Ariadne
from .signature import signature_of

#: Labels tried, in order, for the service an alert fired on.
SERVICE_LABELS = ("service", "app", "job")

#: Alertmanager severities in the wild, onto the graph's four levels.
SEVERITY_MAP = {
    "critical": "critical", "page": "critical", "p1": "critical", "sev1": "critical",
    "high": "high", "error": "high", "p2": "high", "sev2": "high",
    "medium": "medium", "warning": "medium", "warn": "medium", "p3": "medium", "sev3": "medium",
    "low": "low", "info": "low", "none": "low", "p4": "low", "sev4": "low",
}
DEFAULT_SEVERITY = "medium"

#: More alerts than this in one notification is not a page, it is a flood; refuse it whole.
MAX_ALERTS = 100

SOURCE = "alertmanager"

_SERVICE_EXISTS = "MATCH (s:Service {name: $service}) RETURN s.name AS name"

_ALERT_EXISTS = "MATCH (x:Alert {id: $id}) RETURN x.id AS id"

_CREATE_ALERT = """
MATCH (s:Service {name: $service})
CREATE (x:Alert {id: $id, at: $at, metric: $metric, value: $value, severity: $severity,
                 source: $source, alertname: $alertname, fingerprint: $fingerprint,
                 received_at: $received_at})
CREATE (x)-[:FIRED_ON]->(s)
"""

# Go's RFC 3339 carries up to nine fractional digits; Python's parser takes six.
_FRACTION = re.compile(r"(\.\d{6})\d+")


class PayloadError(ValueError):
    """The body is not an Alertmanager notification at all — a 400, not a partial accept."""


def parse_rfc3339(value: str) -> int:
    """`startsAt` as epoch milliseconds. Accepts `Z` or an offset, and nanosecond fractions."""
    text = _FRACTION.sub(r"\1", value.strip())
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError(f"startsAt has no timezone: {value!r}")
    return int(dt.timestamp() * 1000)


def format_rfc3339(ms: int) -> str:
    """Epoch milliseconds as the RFC 3339 string Alertmanager would send."""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def fingerprint_of(labels: dict[str, Any]) -> str:
    """A stable 16-hex fingerprint for a label set, for senders that do not supply one."""
    canon = json.dumps(sorted((str(k), str(v)) for k, v in labels.items()))
    return hashlib.sha1(canon.encode()).hexdigest()[:16]


def alert_id(fingerprint: str, at: int) -> str:
    return "am-" + hashlib.sha1(f"{fingerprint}|{at}".encode()).hexdigest()[:10]


def normalize(entry: Any) -> tuple[dict[str, Any] | None, str | None]:
    """One webhook alert as a graph alert, or `(None, reason)` when it cannot be one."""
    if not isinstance(entry, dict):
        return None, "alert is not an object"
    if entry.get("status") != "firing":
        return None, f"status is {entry.get('status')!r}, not 'firing'"
    labels = entry.get("labels") or {}
    annotations = entry.get("annotations") or {}
    if not isinstance(labels, dict) or not isinstance(annotations, dict):
        return None, "labels and annotations must be objects"

    service = next((str(labels[k]) for k in SERVICE_LABELS if labels.get(k)), None)
    if service is None:
        return None, f"no service label (tried {', '.join(SERVICE_LABELS)})"
    if not entry.get("startsAt"):
        return None, "no startsAt"
    try:
        at = parse_rfc3339(str(entry["startsAt"]))
    except ValueError as exc:
        return None, f"startsAt is not RFC 3339: {exc}"

    raw_severity = str(labels.get("severity", "")).strip().lower()
    severity = SEVERITY_MAP.get(raw_severity, DEFAULT_SEVERITY)
    value: float | None
    try:
        value = float(annotations["value"]) if "value" in annotations else None
    except (TypeError, ValueError):
        value = None

    fingerprint = str(entry.get("fingerprint") or fingerprint_of(labels))
    alertname = str(labels.get("alertname", ""))
    return {
        "id": alert_id(fingerprint, at),
        "at": at,
        "metric": str(labels.get("metric") or alertname or "unknown"),
        "value": value,
        "severity": severity,
        "service": service,
        "alertname": alertname,
        "fingerprint": fingerprint,
        "severity_mapped_from": None if raw_severity in SEVERITY_MAP else (raw_severity or None),
    }, None


def ingest(a: Ariadne, payload: Any) -> dict[str, Any]:
    """Write every firing alert in an Alertmanager notification to the graph.

    Returns what was accepted (with its structural signature, computed now so the caller can see
    whether memory holds a plan for it) and what was skipped and why. Skips are not errors: one
    bad alert in a group must not cost the others.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("alerts"), list):
        raise PayloadError("not an Alertmanager notification: expected an object with 'alerts'")
    alerts = payload["alerts"]
    if len(alerts) > MAX_ALERTS:
        raise PayloadError(f"{len(alerts)} alerts in one notification; the limit is {MAX_ALERTS}")

    accepted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for index, entry in enumerate(alerts):
        alert, reason = normalize(entry)
        if alert is None:
            skipped.append({"index": index, "reason": reason})
            continue
        if a.one(_SERVICE_EXISTS, {"service": alert["service"]}) is None:
            skipped.append({"index": index, "reason": f"unknown service {alert['service']!r}"})
            continue

        duplicate = a.one(_ALERT_EXISTS, {"id": alert["id"]}) is not None
        if not duplicate:
            a.write(_CREATE_ALERT, {**{k: alert[k] for k in (
                "id", "at", "metric", "value", "severity", "service", "alertname",
                "fingerprint")}, "source": SOURCE, "received_at": int(time.time() * 1000)})

        signature, shape = signature_of(a, alert)
        accepted.append({
            "id": alert["id"],
            "service": alert["service"],
            "metric": alert["metric"],
            "severity": alert["severity"],
            "severity_mapped_from": alert["severity_mapped_from"],
            "at": alert["at"],
            "duplicate": duplicate,
            "signature": signature,
            "shape": shape.as_dict(),
            "investigate": f"/api/investigate?alert={alert['id']}",
        })
    return {"accepted": accepted, "skipped": skipped}
