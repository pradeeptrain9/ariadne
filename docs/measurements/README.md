# Measurements

Raw ledger: `runs.jsonl` in this directory, written by `python -m ariadne.replay`.
Every number in the README and the submission comes from this file; none is typed by
hand.

Model **`claude-sonnet-5`**. Each row records its own model, so a mixed ledger can never be
averaged into a meaningless ratio — `ledger.compare` emits a warning instead of a
number.

Cold = memory withheld, warm = memory available, **same alerts** in both arms against
the same deterministic graph. Comparing a warm run on one alert to a cold run on
another would measure the alerts, not the memory.

| mode | runs | tool calls | tokens | cost | wall |
|---|---|---|---|---|---|
| cold | 3 | 7.0 | 36,320 | $0.0503 | 28.0s |
| warm | 3 | 4.0 | 25,867 | $0.0273 | 16.2s |

**1.8x cheaper, 1.8x fewer tool calls when memory is available.**

## Per run

| mode | alert | calls | tokens | cache read | cost | culprit | confidence |
|---|---|---|---|---|---|---|---|
| cold | alr-055 | 7 | 36,226 | 22,140 | $0.0508 | `951e8b261e` | 0.6 |
| cold | alr-a00 | 7 | 37,121 | 22,140 | $0.0548 | `614ca4f7a5` | 0.5 |
| cold | alr-a01 | 7 | 35,614 | 22,140 | $0.0452 | `d524715b1f` | 0.55 |
| warm | alr-055 | 4 | 26,490 | 18,450 | $0.0291 | `951e8b261e` | 0.85 |
| warm | alr-a00 | 4 | 26,069 | 18,450 | $0.0278 | `614ca4f7a5` | 0.8 |
| warm | alr-a01 | 4 | 25,043 | 18,450 | $0.0249 | `d524715b1f` | 0.85 |

## Accuracy guard

3 of 3 alerts run in both arms reached the **same culprit commit**.
A cheap wrong answer is worse than an expensive right one, so `replay.py` asserts
this and exits non-zero on a mismatch, rather than leaving it to inspection.

## Tool usage by mode

- **cold**: `upstream_candidates`, `deploys_in_window`, `suspect_commits`, `blast_radius`, `owner_of`, `runbook_for`, `record_outcome`
- **warm**: `recall_plan`, `deploys_in_window`, `suspect_commits`, `record_outcome`

Cold explores. Warm tests a hypothesis the graph already holds, so it runs
`recall_plan` and then only `deploys_in_window` and `suspect_commits` before
recording — impact, owner and runbook come back inside the recall itself.

## Earlier measurements on Opus 5

An earlier set of ten runs on `claude-opus-5` measured cold $0.1281 / 8.0 calls and
warm $0.0677 / 4.0 calls — a 1.9x ratio against the 1.8x here. The project moved to
Sonnet 5 for cost; the ratio survived the model change, which is the point of
measuring a ratio rather than an absolute. Those runs are not in this file, because
this file is what the current configuration actually produced.
