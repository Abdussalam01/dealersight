"""The "check this finding" workflow: can the evidence on hand support the stated finding?

DealerSight's hardest question is not what the cameras saw, it is what imperfect camera
activity can be made to support. A probable test-drive session is an assumption about time
and sequence — a return matched to the oldest open departure inside a window — so a finding
built on it may or may not survive a different window.

Everything here is computed in application code, reads only, and writes nothing. Each check
reports `pass`, `caution` or `fail` with the numbers behind it. A failed critical check
withholds the verdict instead of reporting a weaker one: the point is to say what the
evidence cannot support, not to find a way to support it.

Amazon Bedrock never runs these checks. It explains the result, and may only cite the
metric ids this module registers in the packet.
"""

from backend import correlation, funnel

# A finding about sales conversion rests on sales records, which Ring cannot observe.
SIMULATED_CAVEAT = ("Sales and finance records are simulated, so this demonstrates the analysis "
                    "workflow rather than a verified dealership outcome.")

PASS, CAUTION, FAIL = "pass", "caution", "fail"


def _check(checks, key, outcome, name, detail, critical=False):
    checks.append({"key": key, "outcome": outcome, "name": name, "detail": detail, "critical": critical})
    return outcome


def _direction(points):
    if points is None:
        return "undefined"
    if points <= -1.0:
        return "declined"
    if points >= 1.0:
        return "rose"
    return "held steady"


def check_finding(conn, dealer_id=None, now=None):
    """Check whether a decline in sales conversion is supported by the evidence on hand.

    Returns the checks performed, the pairing-sensitivity table, and a verdict of
    `supported`, `depends_on_assumption` or `withheld`.
    """
    period_a, period_b = funnel.periods(now)
    dealer = (conn.execute("SELECT * FROM dealer WHERE id = %s", (dealer_id,)).fetchone() if dealer_id
              else conn.execute("SELECT * FROM dealer WHERE is_demo").fetchone())
    if not dealer:
        return {"verdict": "withheld", "withheld_reason": "no dealership selected", "checks": [],
                "sensitivity": [], "caveat": SIMULATED_CAVEAT}

    comparison = funnel.compare(conn, dealer["id"], period_a, period_b)
    a, b = comparison["period_a"], comparison["period_b"]
    checks = []

    # 1. Is there anything to compare in both periods?
    if a["visits"] and b["visits"] and a["probable_test_drives"] and b["probable_test_drives"]:
        _check(checks, "periods_have_data", PASS, "Both periods have usable activity",
               f"{a['visits']} and {b['visits']} visits; {a['probable_test_drives']} and "
               f"{b['probable_test_drives']} probable test-drive sessions.", critical=True)
    else:
        _check(checks, "periods_have_data", FAIL, "Both periods have usable activity",
               "One period has no visits or no probable test-drive sessions, so a conversion "
               "rate cannot be compared.", critical=True)

    # 2. Equal elapsed time, or the comparison favours the longer period.
    if comparison["equal_coverage"]:
        _check(checks, "comparable_coverage", PASS, "Periods cover equal time",
               f"Both periods cover {a['coverage_hours']} hours.", critical=True)
    else:
        _check(checks, "comparable_coverage", FAIL, "Periods cover equal time",
               f"The periods cover {a['coverage_hours']} and {b['coverage_hours']} hours, so counts "
               "are not directly comparable.", critical=True)

    # 3. Did the two periods draw on the same kinds of source?
    def source_kinds(period):
        result = funnel.funnel(conn, dealer["id"], *period)
        return {stage["key"]: frozenset(stage["provenance"]) for stage in result["stages"]}

    kinds_a, kinds_b = source_kinds(period_a), source_kinds(period_b)
    differing = sorted(stage for stage in kinds_a if kinds_a[stage] != kinds_b.get(stage))
    if not differing:
        _check(checks, "comparable_sources", PASS, "Periods draw on the same kinds of source",
               "Every funnel stage is fed by the same kinds of event origin in both periods.")
    else:
        detail = "; ".join(
            f"{stage.replace('_', ' ')}: {' + '.join(sorted(kinds_a[stage])) or 'nothing'} earlier vs "
            f"{' + '.join(sorted(kinds_b.get(stage, ()))) or 'nothing'} recently" for stage in differing)
        _check(checks, "comparable_sources", CAUTION, "Periods draw on the same kinds of source",
               f"The kinds of source differ between the periods — {detail}. A change in what fed a "
               "stage can move a rate on its own.")

    # 4. Conversion needs sales records, which Ring cannot observe.
    if a["sales"] and b["sales"]:
        _check(checks, "business_records_present", PASS, "Business records exist in both periods",
               f"{a['sales']} and {b['sales']} simulated sales records.", critical=True)
    else:
        _check(checks, "business_records_present", FAIL, "Business records exist in both periods",
               "One period has no sales records, so sales conversion is undefined there.", critical=True)

    # 5. Does the finding survive a different pairing assumption?
    table = []
    directions = set()
    for row in correlation.sensitivity(conn, dealer["id"], {"period_a": period_a, "period_b": period_b}, now):
        drives_a = row["counts"]["period_a"]["probable_test_drives"]
        drives_b = row["counts"]["period_b"]["probable_test_drives"]
        # Sales are business records and do not move with the pairing window; only the denominator does.
        rate_a = round(100 * a["sales"] / drives_a, 1) if drives_a else None
        rate_b = round(100 * b["sales"] / drives_b, 1) if drives_b else None
        points = None if rate_a is None or rate_b is None else round(rate_b - rate_a, 1)
        directions.add(_direction(points))
        table.append({
            "setting": row["setting"], "min_seconds": row["min_seconds"], "max_seconds": row["max_seconds"],
            "probable_test_drives_a": drives_a, "probable_test_drives_b": drives_b,
            "unmatched_departures_b": row["counts"]["period_b"]["unmatched_departures"],
            "sales_conversion_a": rate_a, "sales_conversion_b": rate_b,
            "change_points": points, "direction": _direction(points),
        })

    settings = ", ".join(r["setting"] for r in table)
    declined = {row["direction"] == "declined" for row in table}
    per_setting = "; ".join(f"{r['setting']} {r['direction']}" for r in table)
    if declined == {True}:
        _check(checks, "survives_matching_settings", PASS, "The decline survives every tested matching setting",
               f"Sales conversion declined under all {len(table)} settings ({settings}).")
    elif declined == {False}:
        _check(checks, "survives_matching_settings", FAIL, "The decline survives every tested matching setting",
               f"Sales conversion did not decline under any tested setting — {per_setting}.")
    else:
        _check(checks, "survives_matching_settings", CAUTION, "The decline survives every tested matching setting",
               f"The direction changes with the matching window — {per_setting}. The result depends on the "
               "pairing assumption, not on the camera activity alone.")

    failed = [c for c in checks if c["critical"] and c["outcome"] == FAIL]
    if failed:
        verdict, reason = "withheld", failed[0]["detail"]
    elif declined == {False}:
        # The premise itself is not supported. Saying so is the useful answer, not finding a
        # weaker version of the claim that happens to hold.
        observed = next(iter(directions)) if len(directions) == 1 else "did not move consistently"
        verdict, reason = "not_supported", f"Sales conversion {observed} between these periods."
    elif len(declined) > 1:
        verdict, reason = "depends_on_assumption", None
    else:
        verdict, reason = "supported", None

    return {
        "finding": "Sales conversion declined between the two periods.",
        "dealer": dealer["name"],
        "period_a": {"start": period_a[0].isoformat(), "end": period_a[1].isoformat()},
        "period_b": {"start": period_b[0].isoformat(), "end": period_b[1].isoformat()},
        "observed": {"sales_conversion_a": a["rates"]["sales_conversion"],
                     "sales_conversion_b": b["rates"]["sales_conversion"],
                     "direction": _direction(None if a["rates"]["sales_conversion"] is None
                                             or b["rates"]["sales_conversion"] is None
                                             else round(b["rates"]["sales_conversion"] - a["rates"]["sales_conversion"], 1))},
        "checks": checks,
        "sensitivity": table,
        "verdict": verdict,
        "withheld_reason": reason,
        "caveat": SIMULATED_CAVEAT,
        "rule_version": correlation.load_rules()["rule_version"],
    }
