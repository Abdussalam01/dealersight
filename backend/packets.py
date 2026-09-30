"""Metric packets: the only facts the AI analyst is ever given.

Each packet is built here in SQL/Python from the same metrics the dashboard
shows. Percentages, differences and rankings are computed in code. Bedrock
never receives raw database rows: it receives facts derived from the database,
and it does no arithmetic of its own.

Every packet carries a flat `metrics` table keyed by metric id
(e.g. "sales.change_pct"). The model must reference a metric id for each number
it uses, so a correct number attached to the wrong metric, period or dealership
can be rejected.

If a required metric is missing, the packet is marked `insufficient_data` and
the model is not called at all.
"""

from backend import correlation, funnel

PACKET_SCHEMA_VERSION = "2026-09-23.1"

UNITS = {"count": "count", "percent": "percent", "percent_change": "percent_change",
         "percentage_points": "percentage_points"}

QUESTIONS = {
    "q1_conversion_decline": {
        # Wording required by the project rules. DealerSight answers it by reporting what changed.
        "text": "Why did this dealer's conversion rate decline during the selected period?",
        "note": "DealerSight reports what changed alongside the decline; it does not assert a cause.",
        "needs_dealer": True,
    },
    "q2_test_drives_without_sales": {
        "text": "Which dealers had increased probable test-drive activity without a corresponding increase in sales?",
        "needs_dealer": False,
    },
    "q3_promotion_performance": {
        "text": "How did the selected financing promotion perform across the complete funnel?",
        "needs_dealer": False,
    },
}

# How a stage is produced. Origin (where the events came from) is reported separately per packet,
# because a simulated baseline event and a live Ring event both go through the same rules.
STAGE_METHOD = {
    "visits": "inferred by DealerSight rules from camera activity",
    "engagements": "inferred by DealerSight rules from camera activity",
    "probable_test_drives": "inferred by DealerSight rules from camera activity",
    "sales": "simulated business record",
    "finance_deals": "simulated business record",
}
STAGES = list(STAGE_METHOD)
# Keys are the labels the funnel reports per stage.
ORIGIN_LABELS = {"Live Ring Playground": "live Ring Playground events (Ring-originated)",
                 "Ring replay": "replayed recorded Ring events",
                 "Simulated baseline": "simulated baseline events (Ring event shape, not Ring-originated)",
                 "Simulated business data": "simulated business records"}


def _iso(moment):
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def _period(bounds):
    return {"start": _iso(bounds[0]), "end": _iso(bounds[1])}


def origins(funnel_result):
    """Per-stage event origin taken from the funnel that produced the numbers."""
    described = {}
    for stage in funnel_result["stages"]:
        parts = [f"{count} {ORIGIN_LABELS.get(label, label)}" if label in ORIGIN_LABELS else f"{count} {label}"
                 for label, count in stage["provenance"].items()]
        described[stage["key"]] = "; ".join(parts) or "none in this period"
    return described


def _insufficient(question_id, reason, **extra):
    return {"question_id": question_id, "question": QUESTIONS[question_id]["text"],
            "insufficient_data": reason, **extra}


def direction_of(value):
    if value is None:
        return None
    return "increase" if value > 0 else "decrease" if value < 0 else "flat"


def _metric(metrics, metric_id, value, unit, label, scope):
    """Register one addressable fact. `scope` names the dealership and period it belongs to."""
    metrics[metric_id] = {"value": value, "unit": unit, "label": label, "scope": scope,
                          "direction": direction_of(value) if unit in ("percent_change", "percentage_points") else None}


def build(conn, question_id, dealer_id=None, now=None):
    if question_id not in QUESTIONS:
        raise ValueError(f"unknown question: {question_id}")
    period_a, period_b = funnel.periods(now)
    builder = {"q1_conversion_decline": _q1, "q2_test_drives_without_sales": _q2,
               "q3_promotion_performance": _q3}[question_id]
    packet = builder(conn, dealer_id, period_a, period_b)
    packet.setdefault("rule_version", correlation.load_rules()["rule_version"])
    packet.setdefault("packet_schema_version", PACKET_SCHEMA_VERSION)
    packet.setdefault("data_sources", STAGE_METHOD)
    packet.setdefault("caveat", "Aggregate anonymous activity. Ring-derived stages are inferred; sales and finance "
                                "records are simulated. A correlation between stages does not prove a cause.")
    return packet


def _q1(conn, dealer_id, period_a, period_b):
    """Conversion change for one dealership between two periods."""
    question_id = "q1_conversion_decline"
    dealer = conn.execute(
        "SELECT * FROM dealer WHERE id = %s", (dealer_id,)
    ).fetchone() if dealer_id else conn.execute("SELECT * FROM dealer WHERE is_demo").fetchone()
    if not dealer:
        return _insufficient(question_id, "no dealership selected")

    comparison = funnel.compare(conn, dealer["id"], period_a, period_b)
    a, b = comparison["period_a"], comparison["period_b"]
    origin = {"period_a": origins(funnel.funnel(conn, dealer["id"], *period_a)),
              "period_b": origins(funnel.funnel(conn, dealer["id"], *period_b))}
    if not a["visits"] or not b["visits"]:
        return _insufficient(question_id, "no visits recorded in one of the periods",
                             dealer=dealer["name"], period_a=_period(period_a), period_b=_period(period_b))
    for label, period in (("earlier", a), ("recent", b)):
        if not period["probable_test_drives"]:
            return _insufficient(question_id, f"no probable test-drive sessions in the {label} period, so conversion "
                                              "cannot be compared", dealer=dealer["name"])
        if period["rates"]["sales_conversion"] is None:
            return _insufficient(question_id, f"sales conversion is undefined in the {label} period",
                                 dealer=dealer["name"])

    earlier = f"{dealer['name']}, {_period(period_a)['start']} to {_period(period_a)['end']}"
    recent = f"{dealer['name']}, {_period(period_b)['start']} to {_period(period_b)['end']}"
    metrics = {}
    for key in STAGES:
        _metric(metrics, f"{key}.period_a", a[key], "count", f"{key.replace('_', ' ')} (earlier period)", earlier)
        _metric(metrics, f"{key}.period_b", b[key], "count", f"{key.replace('_', ' ')} (recent period)", recent)
        _metric(metrics, f"{key}.change_pct", comparison["change_pct"][key], "percent_change",
                f"change in {key.replace('_', ' ')}", f"{earlier} vs {recent}")
    rates = {}
    for key in a["rates"]:
        points = (None if a["rates"][key] is None or b["rates"][key] is None
                  else round(b["rates"][key] - a["rates"][key], 1))
        rates[key] = {"a": a["rates"][key], "b": b["rates"][key], "change_points": points}
        _metric(metrics, f"{key}.period_a", a["rates"][key], "percent", f"{key.replace('_', ' ')} (earlier period)", earlier)
        _metric(metrics, f"{key}.period_b", b["rates"][key], "percent", f"{key.replace('_', ' ')} (recent period)", recent)
        _metric(metrics, f"{key}.change_points", points, "percentage_points",
                f"change in {key.replace('_', ' ')}", f"{earlier} vs {recent}")

    return {
        "question_id": question_id,
        "question": QUESTIONS[question_id]["text"],
        "dealer": f"{dealer['name']} (fictional dealership)",
        "region": dealer["region"],
        "period_a": _period(period_a),
        "period_b": _period(period_b),
        "stages": {key: {"a": a[key], "b": b[key], "change_pct": comparison["change_pct"][key]} for key in STAGES},
        "rates_pct": rates,
        "event_origins": origin,
        "metrics": metrics,
    }


def _q2(conn, _dealer_id, period_a, period_b):
    """Dealerships ranked by probable test-drive growth, with the matching sales change."""
    question_id = "q2_test_drives_without_sales"
    rows = []
    for dealer in funnel.dealers(conn):
        comparison = funnel.compare(conn, dealer["id"], period_a, period_b)
        drives, sales = comparison["change_pct"]["probable_test_drives"], comparison["change_pct"]["sales"]
        if drives is None or sales is None:
            continue
        rows.append({
            "dealer": dealer["name"], "region": dealer["region"],
            "probable_test_drives": {"a": comparison["period_a"]["probable_test_drives"],
                                     "b": comparison["period_b"]["probable_test_drives"], "change_pct": drives},
            "sales": {"a": comparison["period_a"]["sales"], "b": comparison["period_b"]["sales"], "change_pct": sales},
            "gap_points": round(drives - sales, 1),
        })
    if not rows:
        return _insufficient(question_id, "no dealership has comparable activity in both periods")

    rows.sort(key=lambda row: row["gap_points"], reverse=True)
    thresholds = correlation.load_rules()["patterns"]
    flagged = [row["dealer"] for row in rows
               if row["probable_test_drives"]["change_pct"] >= thresholds["growth_pct"]
               and abs(row["sales"]["change_pct"]) <= thresholds["stable_pct"]]
    metrics = {}
    origin = {"period_a": origins(funnel.funnel(conn, None, *period_a)),
              "period_b": origins(funnel.funnel(conn, None, *period_b))}
    span = f"{_period(period_a)['start']} to {_period(period_b)['end']}"
    for row in rows:
        code = row["dealer"].lower().replace(" ", "_")
        for key in ("probable_test_drives", "sales"):
            _metric(metrics, f"{code}.{key}.period_a", row[key]["a"], "count",
                    f"{row['dealer']} {key.replace('_', ' ')} (earlier period)", row["dealer"])
            _metric(metrics, f"{code}.{key}.period_b", row[key]["b"], "count",
                    f"{row['dealer']} {key.replace('_', ' ')} (recent period)", row["dealer"])
            _metric(metrics, f"{code}.{key}.change_pct", row[key]["change_pct"], "percent_change",
                    f"{row['dealer']} change in {key.replace('_', ' ')}", f"{row['dealer']}, {span}")
        _metric(metrics, f"{code}.gap_points", row["gap_points"], "percentage_points",
                f"{row['dealer']} test-drive growth minus sales growth", f"{row['dealer']}, {span}")

    return {
        "question_id": question_id,
        "question": QUESTIONS[question_id]["text"],
        "period_a": _period(period_a),
        "period_b": _period(period_b),
        "metrics": metrics,
        "event_origins": origin,
        "ranking_rule": "dealerships sorted by probable test-drive growth minus sales growth, computed in application code",
        "threshold_rule": f"flagged when probable test drives rose at least {thresholds['growth_pct']}% "
                          f"and sales stayed within +/-{thresholds['stable_pct']}%",
        "dealers": rows,
        "dealers_matching_threshold": flagged,
    }


def _q3(conn, dealer_id, _period_a, _period_b):
    """Financing promotion measured across the full funnel against an equal period before it.

    Always network-wide, matching the captive-finance screen, so a dealership chosen on another
    tab cannot silently narrow a network-level question (CR-06).
    """
    question_id = "q3_promotion_performance"
    comparison = funnel.promotion_comparison(conn, dealer_id=None)
    if not comparison:
        return _insufficient(question_id, "no financing promotion is configured")
    before, during = comparison["before"], comparison["during"]
    if not before["sales"] or not during["sales"]:
        return _insufficient(question_id, "no simulated sales in one of the periods, so finance penetration "
                                          "cannot be compared")

    metrics = {}
    before_scope = f"before the promotion, {before['start'].strftime('%Y-%m-%d')} to {before['end'].strftime('%Y-%m-%d')}"
    during_scope = f"during the promotion, {during['start'].strftime('%Y-%m-%d')} to {during['end'].strftime('%Y-%m-%d')}"
    for key in STAGES:
        _metric(metrics, f"{key}.before", before[key], "count", f"{key.replace('_', ' ')} before", before_scope)
        _metric(metrics, f"{key}.during", during[key], "count", f"{key.replace('_', ' ')} during", during_scope)
        _metric(metrics, f"{key}.change_pct", comparison["change_pct"][key], "percent_change",
                f"change in {key.replace('_', ' ')}", f"{before_scope} vs {during_scope}")
        _metric(metrics, f"{key}.per_day_before", before["per_day"][key], "count",
                f"{key.replace('_', ' ')} per day before", before_scope)
        _metric(metrics, f"{key}.per_day_during", during["per_day"][key], "count",
                f"{key.replace('_', ' ')} per day during", during_scope)
    _metric(metrics, "finance_penetration.before", before["rates"]["finance_penetration"], "percent",
            "finance penetration before", before_scope)
    _metric(metrics, "finance_penetration.during", during["rates"]["finance_penetration"], "percent",
            "finance penetration during", during_scope)
    _metric(metrics, "finance_penetration.change_points", comparison["finance_penetration_change_pts"],
            "percentage_points", "change in finance penetration", f"{before_scope} vs {during_scope}")

    return {
        "question_id": question_id,
        "question": QUESTIONS[question_id]["text"],
        "scope": "All dealerships in the network (same scope as the captive-finance screen)",
        "metrics": metrics,
        "event_origins": {
            "before": origins(funnel.funnel(conn, None, before["start"], before["end"])),
            "during": origins(funnel.funnel(conn, None, during["start"], during["end"])),
        },
        "promotion": comparison["promotion"]["name"],
        "equal_durations_days": comparison["equal_durations_days"],
        "period_before": {"start": before["start"].strftime("%Y-%m-%d"), "end": before["end"].strftime("%Y-%m-%d")},
        "period_during": {"start": during["start"].strftime("%Y-%m-%d"), "end": during["end"].strftime("%Y-%m-%d")},
        "stages": {key: {"before": before[key], "during": during[key], "change_pct": comparison["change_pct"][key],
                         "per_day_before": before["per_day"][key], "per_day_during": during["per_day"][key]}
                   for key in STAGES},
        "finance_penetration_pct": {
            "before": before["rates"]["finance_penetration"], "during": during["rates"]["finance_penetration"],
            "change_points": comparison["finance_penetration_change_pts"],
            "counts": {
                "before": {"financed": before["finance_deals"], "sales": before["sales"],
                           "text": f"{before['finance_deals']} of {before['sales']} simulated sales financed"},
                "during": {"financed": during["finance_deals"], "sales": during["sales"],
                           "text": f"{during['finance_deals']} of {during['sales']} simulated sales financed"},
            },
        },
    }
