# Measurements

Raw ledger: `runs.jsonl` in this directory, written by `python -m ariadne.replay --n 20`.
Every number in the README and the submission comes from this file; none is typed by
hand. `summary.json` is `ledger.compare` over it, and a test fails if the two disagree.

Model **`claude-sonnet-5`**. Each row records its own model, so a mixed ledger can never be
averaged into a meaningless ratio — `ledger.compare` emits a warning instead of a
number.

Cold = memory withheld, warm = memory available, **same 20 alerts** in both arms against
the same deterministic graph, re-seeded immediately before the run. Comparing a warm run on
one alert to a cold run on another would measure the alerts, not the memory.

| mode | runs | tool calls | tokens | cost | wall |
|---|---|---|---|---|---|
| cold | 20 | 7.3 | 32,503 | $0.0438 | 22.8s |
| warm | 20 | 4.0 | 26,772 | $0.0274 | 13.9s |

**1.6x cheaper, 1.8x fewer tool calls when memory is available.** Per-run cost ranged
$0.0321–$0.0605 cold and $0.0244–$0.0388 warm. The whole run cost $1.42.

## Accuracy guard

20 of 20 alerts run in both arms reached the **same culprit commit**.
A cheap wrong answer is worse than an expensive right one, so `replay.py` asserts
this and exits non-zero on a mismatch, rather than leaving it to inspection.

## Tool usage by mode

- **warm**, all 20 runs: `recall_plan`, `deploys_in_window`, `suspect_commits`, `record_outcome`
- **cold**, most common (8 of 20): `upstream_candidates`, `deploys_in_window`, `suspect_commits`,
  `blast_radius`, `owner_of`, `runbook_for`, `record_outcome`. The rest reorder the same
  tools, and a few call `deploys_in_window` twice — which is where the 7.3 mean comes from.

Cold explores, and its path varies run to run. Warm tests a hypothesis the graph already
holds, so it runs `recall_plan` and then only `deploys_in_window` and `suspect_commits`
before recording — impact, owner and runbook come back inside the recall itself. The warm
path was identical on every alert.

## Per run

| mode | alert | calls | tokens | cache read | cost | culprit | confidence |
|---|---|---|---|---|---|---|---|
| cold | alr-055 | 7 | 35,266 | 19,475 | $0.0516 | `951e8b261e` | 0.86 |
| warm | alr-055 | 4 | 27,665 | 15,580 | $0.0388 | `951e8b261e` | 0.86 |
| cold | alr-a00 | 7 | 35,768 | 23,370 | $0.0446 | `614ca4f7a5` | 0.95 |
| warm | alr-a00 | 4 | 27,316 | 19,475 | $0.0292 | `614ca4f7a5` | 0.95 |
| cold | alr-a01 | 7 | 39,151 | 23,370 | $0.0544 | `d524715b1f` | 0.95 |
| warm | alr-a01 | 4 | 26,288 | 19,475 | $0.0255 | `d524715b1f` | 0.95 |
| cold | alr-a02 | 7 | 35,636 | 23,370 | $0.0433 | `5a1745a82a` | 0.95 |
| warm | alr-a02 | 4 | 26,216 | 19,475 | $0.0245 | `5a1745a82a` | 0.95 |
| cold | alr-a03 | 7 | 37,700 | 23,370 | $0.0605 | `c07926a918` | 0.45 |
| warm | alr-a03 | 4 | 26,640 | 19,475 | $0.0262 | `c07926a918` | 0.45 |
| cold | alr-a04 | 8 | 28,640 | 19,475 | $0.0353 | `838e680f8a` | 0.95 |
| warm | alr-a04 | 4 | 26,105 | 19,475 | $0.0259 | `838e680f8a` | 0.95 |
| cold | alr-a05 | 7 | 33,633 | 23,370 | $0.0390 | `22d1036cfe` | 0.95 |
| warm | alr-a05 | 4 | 25,977 | 19,475 | $0.0251 | `22d1036cfe` | 0.95 |
| cold | alr-a06 | 8 | 39,044 | 23,370 | $0.0523 | `5dc84fab96` | 0.95 |
| warm | alr-a06 | 4 | 26,534 | 19,475 | $0.0264 | `5dc84fab96` | 0.95 |
| cold | alr-a07 | 8 | 33,583 | 19,475 | $0.0597 | `27fa99716e` | 0.95 |
| warm | alr-a07 | 4 | 26,889 | 19,475 | $0.0294 | `27fa99716e` | 0.95 |
| cold | alr-006 | 8 | 31,643 | 19,475 | $0.0442 | `951e8b261e` | 0.86 |
| warm | alr-006 | 4 | 27,490 | 19,475 | $0.0285 | `951e8b261e` | 0.86 |
| cold | alr-026 | 7 | 29,697 | 19,475 | $0.0433 | `05705cbb84` | 0.45 |
| warm | alr-026 | 4 | 26,234 | 19,475 | $0.0271 | `05705cbb84` | 0.45 |
| cold | alr-030 | 8 | 38,646 | 23,370 | $0.0495 | `65fc5ea5e5` | 0.95 |
| warm | alr-030 | 4 | 26,582 | 19,475 | $0.0253 | `65fc5ea5e5` | 0.95 |
| cold | alr-041 | 7 | 29,042 | 19,475 | $0.0384 | `ce94b2b1bc` | 0.95 |
| warm | alr-041 | 4 | 26,729 | 19,475 | $0.0268 | `ce94b2b1bc` | 0.95 |
| cold | alr-042 | 7 | 30,002 | 19,475 | $0.0378 | `d3f960f74a` | 0.82 |
| warm | alr-042 | 4 | 25,960 | 19,475 | $0.0246 | `d3f960f74a` | 0.82 |
| cold | alr-056 | 7 | 29,564 | 19,475 | $0.0376 | `4b413dd33c` | 0.78 |
| warm | alr-056 | 4 | 28,393 | 19,475 | $0.0311 | `4b413dd33c` | 0.78 |
| cold | alr-001 | 7 | 27,981 | 19,475 | $0.0321 | `0a45521ce8` | 0.95 |
| warm | alr-001 | 4 | 26,657 | 19,475 | $0.0261 | `0a45521ce8` | 0.95 |
| cold | alr-002 | 7 | 29,998 | 19,475 | $0.0401 | `323f477ba0` | 0.95 |
| warm | alr-002 | 4 | 27,240 | 19,475 | $0.0267 | `323f477ba0` | 0.95 |
| cold | alr-008 | 8 | 31,541 | 19,475 | $0.0413 | `c624fbab03` | 0.86 |
| warm | alr-008 | 4 | 26,063 | 19,475 | $0.0244 | `c624fbab03` | 0.86 |
| cold | alr-010 | 7 | 29,012 | 19,475 | $0.0382 | `005c831e46` | 0.88 |
| warm | alr-010 | 4 | 27,182 | 19,475 | $0.0286 | `005c831e46` | 0.88 |
| cold | alr-011 | 8 | 24,522 | 15,580 | $0.0332 | `661d8445af` | 0.95 |
| warm | alr-011 | 4 | 27,273 | 19,475 | $0.0279 | `661d8445af` | 0.95 |

## The 3-alert run it replaces

The first measurement on `claude-sonnet-5` used **3 alerts per arm** and measured cold
$0.0503 / 7.0 calls, warm $0.0273 / 4.0 calls — **1.8x** on cost. Widening to 20 alerts
moved the cost ratio to 1.6x; the call ratio held at 1.8x. The headline is the 20-alert
number. The six rows are kept in `runs-3-alerts.jsonl`.

## Earlier measurements on Opus 5

An earlier set of ten runs on `claude-opus-5` measured cold $0.1281 / 8.0 calls and
warm $0.0677 / 4.0 calls — a 1.9x ratio. The project moved to Sonnet 5 for cost. Those runs
are not in this directory, because it holds what the current configuration produced.
