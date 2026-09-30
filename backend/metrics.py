"""Read-only metrics over derived events. All numbers are computed here, never by the AI."""

from backend import correlation, ingest

LABELS = {
    "visit": "Visits",
    "engagement": "Vehicle-area engagement signals",
    "probable_test_drive": "Probable test-drive sessions",
}


def summary(conn):
    counts = dict.fromkeys(LABELS, 0)
    for row in conn.execute("SELECT type, count(*) AS n FROM derived_event GROUP BY type").fetchall():
        counts[row["type"]] = row["n"]
    open_departures = conn.execute("SELECT count(*) AS n FROM test_drive_candidate WHERE status = 'open'").fetchone()["n"]
    session = ingest.current_session(conn)
    min_gap, max_gap = correlation.timing_window(session["timing_profile"])
    return {
        **counts,
        "open_departures": open_departures,
        "counting_since": session["watermark"],
        "timing_profile": session["timing_profile"],
        "test_drive_window_seconds": [int(min_gap.total_seconds()), int(max_gap.total_seconds())],
        "rule_version": correlation.load_rules()["rule_version"],
    }


def visit_count(conn):
    return summary(conn)["visit"]


def hourly_visits(conn, dealer_id=None, start=None, end=None, timezone="UTC", sources=None):
    """Visits by hour of day in the dealership's own timezone (opening-hours traffic profile)."""
    return conn.execute(
        """SELECT EXTRACT(hour FROM started_at AT TIME ZONE %s)::int AS hour, count(*) AS visits
           FROM derived_event
           WHERE type = 'visit' AND (%s::int IS NULL OR dealer_id = %s)
             AND (%s::timestamptz IS NULL OR started_at >= %s) AND (%s::timestamptz IS NULL OR started_at < %s)
             AND (%s::text[] IS NULL OR source = ANY(%s))
           GROUP BY 1 ORDER BY 1""",
        (timezone, dealer_id, dealer_id, start, start, end, end, sources, sources),
    ).fetchall()


def evidence(conn, kind, limit=20, dealer_id=None, start=None, end=None, sources=None):
    """The derived events behind one displayed number, with the events and rule that produced each.

    Filtered by the same dealership, period and source scope as the metric being shown, so the
    rows always explain the number that was clicked (CR-02).
    """
    matching = conn.execute(
        """SELECT count(*) AS n FROM derived_event d
           WHERE d.type = %s AND (%s::int IS NULL OR d.dealer_id = %s)
             AND (%s::timestamptz IS NULL OR d.started_at >= %s) AND (%s::timestamptz IS NULL OR d.started_at < %s)
             AND (%s::text[] IS NULL OR d.source = ANY(%s))""",
        (kind, dealer_id, dealer_id, start, start, end, end, sources, sources),
    ).fetchone()["n"]
    rows = conn.execute(
        """SELECT d.id, d.type, d.started_at, d.ended_at, d.confidence, d.rule_version, d.source, z.name AS zone,
                  dl.name AS dealer,
                  json_agg(json_build_object(
                      'event_ref', right(e.ring_event_id, 8), 'ring_event_type', e.ring_event_type,
                      'position', p.name, 'zone_source', e.zone_source,
                      'started_at', e.started_at, 'ended_at', e.ended_at) ORDER BY e.started_at) AS ring_events
           FROM derived_event d
           JOIN zone z ON z.id = d.zone_id
           LEFT JOIN dealer dl ON dl.id = d.dealer_id
           JOIN raw_event e ON e.id = ANY(d.source_event_ids)
           JOIN camera_position p ON p.id = e.camera_position_id
           WHERE d.type = %s AND (%s::int IS NULL OR d.dealer_id = %s)
             AND (%s::timestamptz IS NULL OR d.started_at >= %s) AND (%s::timestamptz IS NULL OR d.started_at < %s)
             AND (%s::text[] IS NULL OR d.source = ANY(%s))
           GROUP BY d.id, z.name, dl.name
           ORDER BY d.started_at DESC LIMIT %s""",
        (kind, dealer_id, dealer_id, start, start, end, end, sources, sources, limit),
    ).fetchall()
    for row in rows:
        if row["type"] == "probable_test_drive" and row["ended_at"]:
            row["duration_minutes"] = round((row["ended_at"] - row["started_at"]).total_seconds() / 60, 1)
    return {"matching_rows": matching, "shown": len(rows), "rows": rows}
