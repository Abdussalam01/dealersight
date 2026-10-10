# Scenario evaluation

Unit tests prove the parts work. This evaluation asks a different question: **does the whole product
behave sensibly in the situations it exists to handle — including the ones where the honest answer is
that the evidence supports nothing?**

Each scenario builds clearly labelled synthetic data, runs it through the real pipeline (ingestion →
correlation → funnel → pattern detection → finding checks → analyst), and compares what happened with
what should happen. It evaluates both the calculations and the *meaning* of the AI's answer: a reply
can satisfy every schema rule and still be false.

```powershell
python scripts/run_evaluation.py
```

Every scenario runs inside a transaction that is rolled back, so the evaluation is safe to run against
a live database. It writes nothing except its report, `docs/evidence/scenario-evaluation.json`. The same
scenarios run in the test suite (`tests/test_scenarios.py`), so a regression fails the build.

## The scenarios

| Scenario | Expected behaviour | What it guards against |
|---|---|---|
| Activity and sales remain steady | No misleading decline alert | Manufacturing a finding out of noise |
| Probable test-drive activity rises while sales fall | Flag the pattern and show its evidence | Missing the situation the product exists to surface |
| One comparison period has missing data | Explain the gap and withhold unsupported conclusions | Reporting absence of data as a decline |
| Several departures could match a return | Show that the result depends on the matching assumption | Presenting an assumption as an observation |
| Bedrock is unavailable | Return the labelled deterministic fallback | Silence, or an unlabelled guess |
| The model's answer contradicts the figures it cites | Reject the answer on meaning, not just on shape | Trusting a reply because it passed a schema check |

## Why the last two matter most

**Several departures could match a return.** A probable test-drive session is a *rule applied to time and
sequence*: a Return-lane event closes the oldest open Departure-lane event inside a window. The same
vehicle cannot be confirmed anonymously, so the count depends on the window chosen. The scenario builds
four short round trips and re-runs the pairing rule under the three windows in `config/rules.yaml`:

| Setting | Window | Probable test-drive sessions |
|---|---|---|
| strict | 10 min – 60 min | 0 |
| standard | 5 min – 90 min | 0 |
| generous | 2 min – 3 hr | 4 |

Same cameras, same stored events, three different answers. DealerSight reports that rather than quietly
picking one: "Check this finding" in the AI analyst view runs this comparison and says whether a finding
survives it.

**The model's answer contradicts the figures it cites.** The scenario feeds the analyst a reply whose
claim is copied straight out of the metric packet — correct metric id, value, unit and direction — while
the prose says conversion "fell sharply" about a metric that did not move. A validator checking structure
alone accepts it. DealerSight rejects it:

```
answer rejected: summary contradicts sales_conversion.change_points, which shows no material change
```

and shows the labelled deterministic fallback instead.

## What the evaluation does not claim

The scenarios use synthetic data, stored as `simulated_baseline` — Ring event shape, not Ring-originated.
They demonstrate that the analysis behaves correctly on known inputs. They are not evidence about any real
dealership, and sales and finance records are simulated throughout, because Ring cannot observe a sale.
