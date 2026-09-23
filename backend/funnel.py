"""The connected funnel and the three stakeholder views.

Every number here is computed in SQL/Python, never by the AI. The funnel is an
*anonymous operational* funnel: DealerSight compares aggregate activity at each
stage and never identifies a customer or follows one person from a visit to a
sale. A rate with a zero denominator is None, which the UI shows as an em dash.

Stage sources:
  visits, engagements, probable test-drive sessions -> Ring events + DealerSight rules
  sales, finance deals                              -> simulated business records
"""

from datetime import timedelta

from backend import correlation, ingest, seed

SOURCE_LABELS = {
    "ring_live": "Live Ring Playground",
    "ring_replay": "Ring replay",
    "simulated_baseline": "Simulated baseline",
}
STAGES = [
    ("visits", "Visits", "ring"),
    ("engagements", "Vehicle-area engagements", "ring"),
    ("probable_test_drives", "Probable test-drive sessions", "ring"),
    ("sales", "Vehicle sales", "simulated"),
    ("finance_deals", "Captive-finance deals", "simulated"),
]
DERIVED_KEY = {"visit": "visits", "engagement": "engagements", "probable_test_drive": "probable_test_drives"}


def rate(numerator, denominator):
    """Percentage, or None when there is nothing to divide by (shown as an em dash, never 0%)."""
    return None if not denominator else round(100 * numerator / denominator, 1)


def change_pct(before, after):
    return None if not before else round(100 * (after - before) / before, 1)


def funnel(conn, dealer_id=None, start=None, end=None, sources=None):
    """`sources` limits the Ring-derived stages, e.g. ['ring_live'] for this demo session only."""
    counts = {key: 0 for key, _, _ in STAGES}
    provenance = {key: {} for key, _, _ in STAGES}

    for row in conn.execute(
        """SELECT type, source, count(*) AS n FROM derived_event
           WHERE (%s::int IS NULL OR dealer_id = %s) AND (%s::timestamptz IS NULL OR started_at >= %s)
             AND (%s::timestamptz IS NULL OR started_at < %s) AND (%s::text[] IS NULL OR source = ANY(%s))
           GROUP BY type, source""",
        (dealer_id, dealer_id, start, start, end, end, sources, sources),
    ).fetchall():
        key = DERIVED_KEY[row["type"]]
        counts[key] += row["n"]
        provenance[key][SOURCE_LABELS.get(row["source"], row["source"])] = row["n"]

    business_included = sources is None or "simulated_baseline" in sources
    for key, table in (("sales", "sale"), ("finance_deals", "finance_deal")):
        if not business_included:
            provenance[key] = {}   # simulated business records are not part of a live-only view
            continue
        counts[key] = conn.execute(
            f"""SELECT count(*) AS n FROM {table}
                WHERE (%s::int IS NULL OR dealer_id = %s) AND (%s::timestamptz IS NULL OR occurred_at >= %s)
                  AND (%s::timestamptz IS NULL OR occurred_at < %s)""",
            (dealer_id, dealer_id, start, start, end, end),
        ).fetchone()["n"]
        provenance[key] = {"Simulated business data": counts[key]}

    return {
        "stages": [{"key": key, "label": label, "source_type": source_type, "total": counts[key],
                    "provenance": provenance[key]} for key, label, source_type in STAGES],
        "counts": counts,
        "rates": {
            "engagement_rate": rate(counts["engagements"], counts["visits"]),
            "probable_test_drive_rate": rate(counts["probable_test_drives"], counts["engagements"]),
            # With business records out of scope these are not zero, they are unavailable.
            "sales_conversion": rate(counts["sales"], counts["probable_test_drives"]) if business_included else None,
            "finance_penetration": rate(counts["finance_deals"], counts["sales"]) if business_included else None,
        },
        "business_records_included": business_included,
        "period": {"start": start, "end": end},
    }


def periods(now=None):
    """Period A = the 7 seeded days before last week. Period B = the last 7 seeded days plus
    anything live so far today, so a live Ring event shows up in the current period."""
    now = now or ingest.utcnow()
    (start_a, end_a), (start_b, _) = seed.period_bounds(now.date())
    return (start_a, end_a), (start_b, now)


def dealers(conn):
    return conn.execute("SELECT id, code, name, region, is_demo, timezone FROM dealer ORDER BY name").fetchall()


def compare(conn, dealer_id, period_a, period_b):
    """Two periods side by side with percentage changes, for one dealership."""
    a, b = funnel(conn, dealer_id, *period_a), funnel(conn, dealer_id, *period_b)
    return {
        "period_a": {"start": period_a[0], "end": period_a[1], **a["counts"], "rates": a["rates"]},
        "period_b": {"start": period_b[0], "end": period_b[1], **b["counts"], "rates": b["rates"]},
        "change_pct": {key: change_pct(a["counts"][key], b["counts"][key]) for key in a["counts"]},
    }


def network(conn, period_a, period_b):
    """Manufacturer view: aggregates only. Dealer rows sum to region rows, which sum to the network total."""
    rows = []
    for dealer in dealers(conn):
        comparison = compare(conn, dealer["id"], period_a, period_b)
        rows.append({"dealer": dealer["name"], "region": dealer["region"], "is_demo": dealer["is_demo"],
                     **comparison})

    regions = {}
    for row in rows:
        region = regions.setdefault(row["region"], {"region": row["region"], "dealers": 0,
                                                    "period_a": dict.fromkeys(row["period_a"], 0) | {"rates": None},
                                                    "period_b": dict.fromkeys(row["period_b"], 0) | {"rates": None}})
        region["dealers"] += 1
        for period in ("period_a", "period_b"):
            for key in ("visits", "engagements", "probable_test_drives", "sales", "finance_deals"):
                region[period][key] += row[period][key]

    for region in regions.values():
        for period in ("period_a", "period_b"):
            region[period].pop("start", None), region[period].pop("end", None)
            region[period]["rates"] = _rates(region[period])
        region["change_pct"] = {key: change_pct(region["period_a"][key], region["period_b"][key])
                                for key in ("visits", "engagements", "probable_test_drives", "sales", "finance_deals")}

    total_a = funnel(conn, None, *period_a)
    total_b = funnel(conn, None, *period_b)
    return {
        "dealers": rows,
        "regions": sorted(regions.values(), key=lambda r: r["region"]),
        "network": {"period_a": total_a["counts"] | {"rates": total_a["rates"]},
                    "period_b": total_b["counts"] | {"rates": total_b["rates"]},
                    "change_pct": {k: change_pct(total_a["counts"][k], total_b["counts"][k]) for k in total_a["counts"]}},
        "period_a": {"start": period_a[0], "end": period_a[1]},
        "period_b": {"start": period_b[0], "end": period_b[1]},
    }


def _rates(counts):
    return {
        "engagement_rate": rate(counts["engagements"], counts["visits"]),
        "probable_test_drive_rate": rate(counts["probable_test_drives"], counts["engagements"]),
        "sales_conversion": rate(counts["sales"], counts["probable_test_drives"]),
        "finance_penetration": rate(counts["finance_deals"], counts["sales"]),
    }


def promotion_comparison(conn, code=None, dealer_id=None):
    """Finance view: equal-length periods before and during a financing promotion."""
    promotion = conn.execute(
        "SELECT * FROM promotion WHERE (%s::text IS NULL OR code = %s) ORDER BY starts_at DESC LIMIT 1", (code, code)
    ).fetchone()
    if not promotion:
        return None
    duration = promotion["ends_at"] - promotion["starts_at"]
    before = (promotion["starts_at"] - duration, promotion["starts_at"])
    during = (promotion["starts_at"], promotion["ends_at"])
    days = max(1, duration.days)

    periods = {}
    for name, bounds in (("before", before), ("during", during)):
        data = funnel(conn, dealer_id, *bounds)
        periods[name] = {
            "start": bounds[0], "end": bounds[1], **data["counts"], "rates": data["rates"],
            "per_day": {key: round(value / days, 2) for key, value in data["counts"].items()},
        }
    return {
        "promotion": {"name": promotion["name"], "code": promotion["code"],
                      "starts_at": promotion["starts_at"], "ends_at": promotion["ends_at"], "source": "simulated"},
        "equal_durations_days": days,
        **periods,
        "change_pct": {key: change_pct(periods["before"][key], periods["during"][key])
                       for key in ("visits", "engagements", "probable_test_drives", "sales", "finance_deals")},
        "finance_penetration_change_pts": None if periods["before"]["rates"]["finance_penetration"] is None
        else round((periods["during"]["rates"]["finance_penetration"] or 0)
                   - periods["before"]["rates"]["finance_penetration"], 1),
        "note": "Simulated business data. A difference between periods does not prove the promotion caused it.",
    }


def patterns(conn, period_a, period_b):
    """Situations worth investigating, detected from the numbers. Describes what changed, never why."""
    thresholds = correlation.load_rules()["patterns"]
    found = []
    for dealer in dealers(conn):
        comparison = compare(conn, dealer["id"], period_a, period_b)
        drives = comparison["change_pct"]["probable_test_drives"]
        sales = comparison["change_pct"]["sales"]
        if drives is None or sales is None:
            continue
        facts = {"probable_test_drives_change_pct": drives, "sales_change_pct": sales,
                 "probable_test_drives": [comparison["period_a"]["probable_test_drives"],
                                          comparison["period_b"]["probable_test_drives"]],
                 "sales": [comparison["period_a"]["sales"], comparison["period_b"]["sales"]]}
        if abs(drives) <= thresholds["stable_pct"] and sales <= -thresholds["sales_drop_pct"]:
            found.append({"dealer": dealer["name"], "pattern": "sales_fell_while_test_drives_held",
                          "description": f"Probable test drives held steady ({drives:+.1f}%) while sales fell "
                                         f"{abs(sales):.1f}%.", **facts})
        elif drives >= thresholds["growth_pct"] and abs(sales) <= thresholds["stable_pct"]:
            found.append({"dealer": dealer["name"], "pattern": "test_drives_grew_without_sales",
                          "description": f"Probable test drives rose {drives:.1f}% while sales stayed flat "
                                         f"({sales:+.1f}%).", **facts})
    promotion = promotion_comparison(conn)
    if promotion and promotion["finance_penetration_change_pts"] is not None:
        shift = promotion["finance_penetration_change_pts"]
        if abs(shift) >= 1:
            found.append({"dealer": "Network", "pattern": "finance_penetration_shift",
                          "description": f"Finance penetration was {abs(shift):.1f} points "
                                         f"{'higher' if shift > 0 else 'lower'} during "
                                         f"{promotion['promotion']['name']} than in the equal period before it.",
                          "before_pct": promotion["before"]["rates"]["finance_penetration"],
                          "during_pct": promotion["during"]["rates"]["finance_penetration"]})
    return found
