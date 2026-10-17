"""The measurement harness — the Track 02 claim, computed.

Runs the same alerts twice: once with memory withheld (`cold`) and once with memory available
(`warm`). Same alerts, same graph, same seed, so the only variable is whether the agent could
recall a plan.

Two guards make the result mean something:

* **accuracy** — a warm run must reach the same culprit commit as its cold counterpart. A cheap
  wrong answer is worse than an expensive right one, so this is asserted rather than eyeballed.
* **determinism** — the graph comes from a fixed seed (`python -m ariadne.seed`, run before this
  harness, not by it), so a re-measurement is against the same world rather than a new one.

Everything printed comes from `runs.jsonl`. No number here is typed by hand.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
from typing import Any

from . import ledger as L
from .agent import MODEL, investigate
from .graph import Ariadne


def pick_alerts(a: Ariadne, n: int) -> list[dict[str, Any]]:
    """Alerts whose shape has a plan behind it — the population where memory can help at all."""
    return a.q("""
        MATCH (i:Incident)-[:SOLVED_BY]->(p:Plan)
        MATCH (i)-[tb:TRIGGERED_BY]->(x:Alert)-[fo:FIRED_ON]->(s:Service)
        WHERE ID(tb) >= 0 AND ID(fo) >= 0
        WITH x, s, p.runs AS runs
        RETURN DISTINCT x.id AS id, x.at AS at, x.metric AS metric, x.value AS value,
               x.severity AS severity, s.name AS service
        ORDER BY runs DESC, id
        LIMIT $n
    """, {"n": int(n)})


#: How many alerts could be measured at all: the population `pick_alerts` draws from. Reported
#: by the pre-flight so `--n` can be set against a real number instead of a hopeful one.
_POOL_WITH_PLANS = """
    MATCH (i:Incident)-[:SOLVED_BY]->(p:Plan)
    MATCH (i)-[tb:TRIGGERED_BY]->(x:Alert)-[fo:FIRED_ON]->(s:Service)
    WHERE ID(tb) >= 0 AND ID(fo) >= 0
    RETURN count(DISTINCT x.id) AS c
"""


def done_arms(ledger: Path) -> set[tuple[str, str]]:
    """The (alert, mode) pairs a ledger already holds.

    A 20-alert measurement is 40 model runs and tens of minutes. Losing all of it to a timeout
    at run 31 — and paying for those 30 again — is avoidable, so `--resume` skips what is
    already recorded instead of starting over.
    """
    return {(r["alert_id"], r["mode"]) for r in L.load(ledger)}


def culprits(ledger: Path) -> dict[str, dict[str, Any]]:
    """Culprit per alert per mode, from the ledger rather than from this session's objects.

    Reading the file means the accuracy guard covers a resumed run and `--report-only` too; the
    in-memory version could only check the arms one invocation happened to run, and reported
    nothing at all when both arms came from different sessions.
    """
    out: dict[str, dict[str, Any]] = {}
    for row in L.load(ledger):  # later rows win, so a re-run replaces its own earlier result
        out.setdefault(row["alert_id"], {})[row["mode"]] = row.get("culprit")
    return out


def mismatches(ledger: Path) -> list[dict[str, Any]]:
    """Alerts where the warm arm reached a different commit than the cold one."""
    return [
        {"alert": aid, "cold": arms["cold"], "warm": arms["warm"]}
        for aid, arms in sorted(culprits(ledger).items())
        if "cold" in arms and "warm" in arms and arms["cold"] != arms["warm"]
    ]


def preflight(n: int, *, ledger: Path, resume: bool = False,
              graph: Ariadne | None = None) -> dict[str, Any]:
    """Everything that can be checked before spending anything.

    A measurement run is the one expensive thing in this project, and every way it can fail is
    knowable in advance: too few alerts in the graph, a model with no price recorded, no
    credential. Checking them after the first API call means finding out at the end of a long
    job, or paying for a run that cannot be costed. This needs no API key, so
    `--dry-run` reports exactly what a real run would do while the key is still missing.
    """
    if graph is not None:
        alerts, pool = pick_alerts(graph, n), graph.scalar(_POOL_WITH_PLANS) or 0
    else:
        with Ariadne() as a:
            alerts, pool = pick_alerts(a, n), a.scalar(_POOL_WITH_PLANS) or 0

    try:
        priced, price_error = bool(L.prices_for(MODEL)), None
    except KeyError as exc:
        priced, price_error = False, str(exc)

    already = done_arms(ledger) if resume else set()
    planned = [(alert["id"], mode) for mode in ("cold", "warm") for alert in alerts]
    todo = [p for p in planned if p not in already]

    # Cost estimate from runs that actually happened, never from a guess: this machine's ledger
    # if it holds any, else the measurements the repo ships. With neither there is no basis for
    # an estimate, and the field stays null rather than inventing a rate.
    basis = ledger if L.load(ledger) else L.MEASURED_LEDGER
    prior = L.compare(L.load(basis))
    per_mode = {m: prior.get(m, {}).get("mean_cost_usd") for m in ("cold", "warm")}
    estimate = (round(sum(per_mode[m] for _aid, m in todo), 2)
                if all(per_mode.values()) else None)

    checks = {
        "alerts_requested": n,
        "alerts_available": len(alerts),
        "alerts_with_a_plan_in_graph": pool,
        "model": MODEL,
        "model_priced": priced,
        "credential_present": bool(os.environ.get("ANTHROPIC_API_KEY")
                                   or os.environ.get("ANTHROPIC_AUTH_TOKEN")),
        "ledger": str(ledger),
        "runs_planned": len(planned),
        "runs_already_recorded": len(planned) - len(todo),
        "runs_to_make": len(todo),
        "estimated_cost_usd": estimate,
        "estimate_basis": str(basis),
    }
    problems = []
    if not alerts:
        problems.append("no alerts with a plan behind them; run `python -m ariadne.seed`")
    elif len(alerts) < n:
        problems.append(f"asked for {n} alerts per arm but the graph only offers {len(alerts)}; "
                        f"the run would silently measure {len(alerts)}")
    if not priced:
        problems.append(price_error or f"no price recorded for {MODEL}")
    if not checks["credential_present"]:
        problems.append("no ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) in the environment or "
                        ".env; the first investigation would fail on authentication")
    checks["problems"] = problems
    return checks


def run(n: int, *, ledger: Path, verbose: bool = True, resume: bool = False,
        graph: Ariadne | None = None) -> dict[str, Any]:
    alerts_seen: set[str] = set()
    already = done_arms(ledger) if resume else set()

    with contextlib.ExitStack() as stack:
        # A caller-supplied graph is not ours to close; one we open is.
        a = graph if graph is not None else stack.enter_context(Ariadne())
        alerts = pick_alerts(a, n)
        if not alerts:
            raise SystemExit("no alerts with a plan behind them; run `python -m ariadne.seed`")

        for mode in ("cold", "warm"):
            print(f"\n=== {mode} ===")
            for alert in alerts:
                alerts_seen.add(alert["id"])
                if (alert["id"], mode) in already:
                    print(f"alert {alert['id']} {mode}: already in the ledger, skipping")
                    continue
                investigate(a, alert, ledger=ledger, force_mode=mode, verbose=verbose)

    summary = L.compare(L.load(ledger))
    summary["alerts"] = len(alerts_seen)
    summary["culprit_mismatches"] = mismatches(ledger)
    return summary


def report(ledger: Path = L.LEDGER_PATH) -> dict[str, Any]:
    """Aggregate an existing ledger without running anything."""
    summary = L.compare(L.load(ledger))
    summary["culprit_mismatches"] = mismatches(ledger)
    return summary


def _print(summary: dict[str, Any]) -> None:
    cold, warm = summary.get("cold", {}), summary.get("warm", {})
    print("\n" + "=" * 62)
    print(f"{'':10} {'runs':>5} {'calls':>7} {'tokens':>9} {'cost':>9} {'wall':>8}")
    for name, agg in (("cold", cold), ("warm", warm)):
        if not agg.get("runs"):
            continue
        print(f"{name:10} {agg['runs']:5d} {agg['mean_tool_calls']:7.1f} "
              f"{agg['mean_tokens']:9,d} ${agg['mean_cost_usd']:8.4f} "
              f"{agg['mean_wall_ms']:7,d}ms")
    if "cost_ratio" in summary:
        print(f"\ncost ratio  {summary['cost_ratio']}x cheaper warm")
        print(f"call ratio  {summary['call_ratio']}x fewer tool calls warm")
    mism = summary.get("culprit_mismatches")
    if mism is None:
        pass
    elif not summary.get("cold", {}).get("runs") or not summary.get("warm", {}).get("runs"):
        print("accuracy    not checked: the ledger does not hold both arms")
    elif mism:
        print(f"\nACCURACY FAILURE: {len(mism)} warm run(s) reached a different culprit")
        for m in mism:
            print(f"  {m['alert']}: cold {m['cold']} vs warm {m['warm']}")
    else:
        print("accuracy    warm runs reached the same culprit as cold on every alert")
    print("=" * 62)


def main() -> None:
    ap = argparse.ArgumentParser(description="Measure cold vs warm cost.")
    ap.add_argument("--n", type=int, default=5, help="how many alerts to run in each arm")
    ap.add_argument("--ledger", type=Path, default=L.LEDGER_PATH)
    ap.add_argument("--report-only", action="store_true", help="aggregate an existing ledger")
    ap.add_argument("--dry-run", action="store_true",
                    help="check what the run would do, and cost, without calling the model")
    ap.add_argument("--resume", action="store_true",
                    help="keep the ledger and skip the (alert, mode) pairs already in it")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.report_only:
        summary = report(args.ledger)
    else:
        checks = preflight(args.n, ledger=args.ledger, resume=args.resume)
        print(json.dumps(checks, indent=2))
        for problem in checks["problems"]:
            print(f"  ! {problem}")
        if args.dry_run:
            # A dry run reports; it never writes a ledger row. Rows no model produced would be
            # indistinguishable from measurements in the file the submission quotes.
            raise SystemExit(1 if checks["problems"] else 0)
        if checks["problems"]:
            raise SystemExit("refusing to start: " + "; ".join(checks["problems"]))

        if args.ledger.exists() and not args.resume:
            args.ledger.unlink()  # a fresh measurement, not an append to an old one
        summary = run(args.n, ledger=args.ledger, verbose=not args.quiet, resume=args.resume)

    _print(summary)
    print(json.dumps({k: v for k, v in summary.items()
                      if k not in ("culprit_mismatches",)}, indent=2))
    if summary.get("culprit_mismatches"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
