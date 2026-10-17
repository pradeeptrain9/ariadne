"""The measurement harness, checked without spending a measurement.

The expensive thing in this project is a measurement run: 20 alerts per arm is 40 model calls
and tens of minutes. Everything about that run which can be wrong *before* the first call —
too few alerts in the graph, an unpriced model, a missing credential, a resume that re-runs
what it already has, an accuracy guard that silently checks nothing — is checked here, where it
costs nothing.
"""

from __future__ import annotations

import json

import pytest

from ariadne import ledger as L
from ariadne import replay


def _ledger(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def _row(alert, mode, culprit="abc123", cost=0.05, calls=7):
    return {"alert_id": alert, "mode": mode, "culprit": culprit, "cost_usd": cost,
            "tool_calls": calls, "total_tokens": 30_000, "wall_ms": 20_000,
            "model": "claude-sonnet-5", "signature": "sig", "effort": "high"}


# --- resume --------------------------------------------------------------------------------


def test_done_arms_reads_the_pairs_already_recorded(tmp_path):
    led = _ledger(tmp_path / "runs.jsonl",
                  [_row("alr-1", "cold"), _row("alr-1", "warm"), _row("alr-2", "cold")])
    assert replay.done_arms(led) == {("alr-1", "cold"), ("alr-1", "warm"), ("alr-2", "cold")}


def test_done_arms_of_a_missing_ledger_is_empty(tmp_path):
    assert replay.done_arms(tmp_path / "nope.jsonl") == set()


def test_a_fully_recorded_resume_makes_no_model_call(seeded, tmp_path):
    """The point of `--resume`: a run that already has every arm must cost nothing. There is no
    credential in this environment, so if it tried to call the model this would raise."""
    alerts = replay.pick_alerts(seeded, 2)
    rows = [_row(a["id"], mode) for mode in ("cold", "warm") for a in alerts]
    led = _ledger(tmp_path / "runs.jsonl", rows)

    summary = replay.run(2, ledger=led, resume=True, graph=seeded, verbose=False)

    assert summary["alerts"] == 2
    assert summary["cold"]["runs"] == 2 and summary["warm"]["runs"] == 2
    assert len(L.load(led)) == len(rows), "a resumed run must not append a duplicate"


# --- the accuracy guard --------------------------------------------------------------------


def test_mismatch_is_reported_per_alert(tmp_path):
    led = _ledger(tmp_path / "runs.jsonl", [
        _row("alr-1", "cold", culprit="aaa"), _row("alr-1", "warm", culprit="aaa"),
        _row("alr-2", "cold", culprit="bbb"), _row("alr-2", "warm", culprit="ccc"),
    ])
    assert replay.mismatches(led) == [{"alert": "alr-2", "cold": "bbb", "warm": "ccc"}]


def test_a_structural_verdict_matches_a_structural_verdict(tmp_path):
    """`culprit: null` in both arms is agreement, not a mismatch."""
    led = _ledger(tmp_path / "runs.jsonl",
                  [_row("alr-1", "cold", culprit=None), _row("alr-1", "warm", culprit=None)])
    assert replay.mismatches(led) == []


def test_an_alert_run_in_only_one_arm_is_not_a_mismatch(tmp_path):
    led = _ledger(tmp_path / "runs.jsonl", [_row("alr-1", "cold", culprit="aaa")])
    assert replay.mismatches(led) == []


def test_report_only_now_carries_the_accuracy_guard(tmp_path):
    """It used to aggregate a ledger and say nothing about whether the arms agreed."""
    led = _ledger(tmp_path / "runs.jsonl", [
        _row("alr-1", "cold", culprit="bbb"), _row("alr-1", "warm", culprit="ccc"),
    ])
    assert replay.report(led)["culprit_mismatches"] == [
        {"alert": "alr-1", "cold": "bbb", "warm": "ccc"}]


def test_the_shipped_measurements_agree_across_arms(tmp_path):
    """The numbers the submission quotes, re-checked from the file rather than from its prose."""
    assert replay.mismatches(L.MEASURED_LEDGER) == []
    summary = replay.report(L.MEASURED_LEDGER)
    assert summary["cold"]["runs"] == summary["warm"]["runs"] > 0
    assert summary["models"] == ["claude-sonnet-5"], "a mixed-model ledger has no valid ratio"


# --- pre-flight ----------------------------------------------------------------------------


def test_preflight_counts_the_runs_it_would_make(seeded, tmp_path):
    checks = replay.preflight(5, ledger=tmp_path / "runs.jsonl", graph=seeded)
    assert checks["alerts_available"] == 5
    assert checks["runs_planned"] == 10
    assert checks["runs_to_make"] == 10
    assert checks["runs_already_recorded"] == 0
    assert checks["model_priced"] is True


def test_preflight_subtracts_what_a_resume_would_skip(seeded, tmp_path):
    alerts = replay.pick_alerts(seeded, 3)
    led = _ledger(tmp_path / "runs.jsonl", [_row(a["id"], "cold") for a in alerts])
    checks = replay.preflight(3, ledger=led, resume=True, graph=seeded)
    assert checks["runs_planned"] == 6
    assert checks["runs_already_recorded"] == 3
    assert checks["runs_to_make"] == 3


def test_preflight_refuses_to_silently_measure_fewer_alerts(seeded, tmp_path):
    """Asking for more alerts than the graph can offer used to shrink the sample without
    saying so — the one failure mode that corrupts the result rather than stopping it."""
    too_many = 10_000
    checks = replay.preflight(too_many, ledger=tmp_path / "runs.jsonl", graph=seeded)
    assert checks["alerts_available"] < too_many
    assert any("silently measure" in p for p in checks["problems"])


def test_preflight_names_the_missing_credential(seeded, tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    checks = replay.preflight(2, ledger=tmp_path / "runs.jsonl", graph=seeded)
    assert checks["credential_present"] is False
    assert any("ANTHROPIC_API_KEY" in p for p in checks["problems"])


def test_preflight_is_clean_once_a_credential_exists(seeded, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    checks = replay.preflight(2, ledger=tmp_path / "runs.jsonl", graph=seeded)
    assert checks["credential_present"] is True
    assert checks["problems"] == []


def test_the_cost_estimate_comes_from_measured_runs(seeded, tmp_path):
    """An estimate is only honest if something produced it. With an empty local ledger that is
    the committed measurement file; the estimate names whichever it used."""
    checks = replay.preflight(2, ledger=tmp_path / "runs.jsonl", graph=seeded)
    shipped = L.compare(L.load(L.MEASURED_LEDGER))
    expected = 2 * (shipped["cold"]["mean_cost_usd"] + shipped["warm"]["mean_cost_usd"])
    assert checks["estimated_cost_usd"] == pytest.approx(round(expected, 2))
    assert checks["estimate_basis"] == str(L.MEASURED_LEDGER)


def test_an_estimate_with_nothing_to_base_it_on_is_null(seeded, tmp_path, monkeypatch):
    monkeypatch.setattr(L, "MEASURED_LEDGER", tmp_path / "absent.jsonl")
    checks = replay.preflight(2, ledger=tmp_path / "runs.jsonl", graph=seeded)
    assert checks["estimated_cost_usd"] is None
