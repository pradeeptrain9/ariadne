"""Alertmanager webhook ingestion, against the real seeded graph and a real HTTP server.

The webhook is the one route on the demo server that writes from outside, so the properties
that matter are the receiver's: a re-sent notification must not duplicate an alert, one bad
alert must not cost the others in its group, a resolved alert must not be filed as a page, and
a body that is not a notification at all must be refused rather than half-read.
"""

from __future__ import annotations

import http.client
import json
import threading
from functools import partial
from http.server import ThreadingHTTPServer

import pytest

from ariadne import fire
from ariadne.ingest import MAX_ALERTS, PayloadError, ingest, parse_rfc3339
from ariadne.server import Handler, MAX_BODY, demo_alerts
from ariadne.signature import signature_of


@pytest.fixture
def g(seeded):
    """The session graph, with every webhook alert removed afterwards: other tests count alerts."""
    yield seeded
    seeded.write("MATCH (x:Alert {source: 'alertmanager'}) DETACH DELETE x")


def payload(service="catalog-service", *, severity="high", status="firing", at=None,
            g=None, **labels):
    at = at if at is not None else fire.starts_at_on_graph_clock(g, service)
    body = fire.build_payload(service=service, metric="latency_p99", severity=severity,
                              value=18.4, starts_at_ms=at)
    body["alerts"][0]["status"] = status
    body["alerts"][0]["labels"].update(labels)
    return body


def stored(g, alert_id):
    return g.one("""MATCH (x:Alert {id: $id})-[:FIRED_ON]->(s:Service)
                    RETURN x.at AS at, x.metric AS metric, x.severity AS severity,
                           x.value AS value, x.source AS source, s.name AS service""",
                 {"id": alert_id})


# --- mapping -------------------------------------------------------------------------------


def test_a_firing_alert_becomes_an_alert_on_its_service(g):
    body = payload(g=g)
    result = ingest(g, body)
    assert result["skipped"] == [] and len(result["accepted"]) == 1
    got = result["accepted"][0]
    row = stored(g, got["id"])
    assert row == {"at": parse_rfc3339(body["alerts"][0]["startsAt"]), "metric": "latency_p99",
                   "severity": "high", "value": 18.4, "source": "alertmanager",
                   "service": "catalog-service"}


def test_the_signature_is_the_one_the_agent_will_compute(g):
    """The response promises a memory hit or miss before anyone pays for an investigation; it
    must be the same signature `investigate` computes from the stored alert."""
    got = ingest(g, payload(g=g))["accepted"][0]
    row = stored(g, got["id"])
    assert got["signature"] == signature_of(g, {**row, "id": got["id"]})[0]


def test_the_demo_alert_has_scenario_a_s_shape(g):
    """`ariadne.fire`'s default lands on the graph's clock inside catalog-service's latest deploy
    window — the shape scenario A1 has, which is what makes the warm shot of the video a hit."""
    got = ingest(g, payload(g=g))["accepted"][0]
    scenario_a1 = json.load(open("fixtures/scenarios.json"))["scenario_a"][0]
    assert got["shape"]["deploy_in_window"] is True
    assert got["signature"] == signature_of(g, scenario_a1)[0]


def test_a_resent_notification_is_acknowledged_not_duplicated(g):
    """Alertmanager re-sends an unresolved alert every repeat_interval."""
    body = payload(g=g)
    first = ingest(g, body)["accepted"][0]
    second = ingest(g, body)["accepted"][0]
    assert first["id"] == second["id"]
    assert (first["duplicate"], second["duplicate"]) == (False, True)
    assert g.scalar("MATCH (x:Alert {id: $id}) RETURN count(x) AS c", {"id": first["id"]}) == 1


def test_a_new_firing_of_the_same_label_set_is_a_new_alert(g):
    at = fire.starts_at_on_graph_clock(g, "catalog-service")
    a = ingest(g, payload(at=at, g=g))["accepted"][0]
    b = ingest(g, payload(at=at + 60_000, g=g))["accepted"][0]
    assert a["id"] != b["id"]


@pytest.mark.parametrize("raw,expected,flagged", [
    ("critical", "critical", None), ("warning", "medium", None), ("info", "low", None),
    ("P2", "high", None), ("bananas", "medium", "bananas"),
])
def test_severity_maps_onto_the_graph_s_four_levels(g, raw, expected, flagged):
    got = ingest(g, payload(severity=raw, g=g))["accepted"][0]
    assert got["severity"] == expected
    assert got["severity_mapped_from"] == flagged


def test_one_bad_alert_does_not_cost_the_rest_of_its_group(g):
    body = payload(g=g)
    good = body["alerts"][0]
    body["alerts"] = [
        {**good, "status": "resolved"},
        {**good, "labels": {**good["labels"], "service": "no-such-service"}},
        {**good, "labels": {k: v for k, v in good["labels"].items() if k != "service"}},
        {**good, "startsAt": "yesterday"},
        good,
    ]
    result = ingest(g, body)
    assert len(result["accepted"]) == 1
    reasons = [s["reason"] for s in result["skipped"]]
    assert [s["index"] for s in result["skipped"]] == [0, 1, 2, 3]
    assert "not 'firing'" in reasons[0]
    assert "unknown service" in reasons[1]
    assert "no service label" in reasons[2]
    assert "RFC 3339" in reasons[3]
    assert g.scalar("MATCH (x:Alert {source: 'alertmanager'}) RETURN count(x) AS c") == 1


def test_the_service_label_falls_back_to_app_then_job(g):
    body = payload(g=g)
    labels = body["alerts"][0]["labels"]
    labels["job"] = labels.pop("service")
    assert ingest(g, body)["accepted"][0]["service"] == "catalog-service"


@pytest.mark.parametrize("bad", [None, [], {"alerts": "x"}, {"status": "firing"}])
def test_a_body_that_is_not_a_notification_is_refused(g, bad):
    with pytest.raises(PayloadError):
        ingest(g, bad)


def test_a_flood_is_refused_whole(g):
    body = payload(g=g)
    body["alerts"] = body["alerts"] * (MAX_ALERTS + 1)
    with pytest.raises(PayloadError):
        ingest(g, body)
    assert g.scalar("MATCH (x:Alert {source: 'alertmanager'}) RETURN count(x) AS c") == 0


@pytest.mark.parametrize("text,ms", [
    ("2025-10-08T21:05:20Z", 1759957520000),
    ("2025-10-08T21:05:20.123456789Z", 1759957520123),
    ("2025-10-09T02:35:20+05:30", 1759957520000),
])
def test_starts_at_parses_go_rfc3339(text, ms):
    assert parse_rfc3339(text) == ms


def test_fire_round_trips_its_own_timestamp():
    ms = 1759957520123
    assert parse_rfc3339(fire.build_payload(
        service="x", metric="latency_p99", severity="high", value=1.0,
        starts_at_ms=ms)["alerts"][0]["startsAt"]) == ms


# --- the picker ----------------------------------------------------------------------------


def test_pushed_alerts_lead_the_picker_and_leave_the_seeded_list(g):
    got = ingest(g, payload(g=g))["accepted"][0]
    alerts = demo_alerts(g)
    assert [a["id"] for a in alerts["pushed"]] == [got["id"]]
    assert got["id"] not in {a["id"] for a in alerts["unresolved"]}


# --- over HTTP -----------------------------------------------------------------------------


@pytest.fixture
def server(g):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, graph=g, ledger=None))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def post(httpd, body: bytes, path="/api/alertmanager", headers=None):
    conn = http.client.HTTPConnection(*httpd.server_address, timeout=30)
    conn.request("POST", path, body=body,
                 headers={"Content-Type": "application/json", **(headers or {})})
    resp = conn.getresponse()
    data = json.loads(resp.read().decode())
    conn.close()
    return resp.status, data


def test_the_webhook_accepts_a_notification_over_http(g, server):
    status, data = post(server, json.dumps(payload(g=g)).encode())
    assert status == 200
    assert data["accepted"][0]["service"] == "catalog-service"
    assert stored(g, data["accepted"][0]["id"]) is not None


def test_the_webhook_refuses_what_is_not_a_notification(g, server):
    assert post(server, b"{not json")[0] == 400
    assert post(server, b'{"hello": "world"}')[0] == 400
    assert post(server, b"{}", path="/api/elsewhere")[0] == 404


def test_the_webhook_refuses_an_oversized_body_before_reading_it(g, server):
    conn = http.client.HTTPConnection(*server.server_address, timeout=30)
    conn.putrequest("POST", "/api/alertmanager")
    conn.putheader("Content-Type", "application/json")
    conn.putheader("Content-Length", str(MAX_BODY + 1))
    conn.endheaders()
    resp = conn.getresponse()
    assert resp.status == 413
    conn.close()
