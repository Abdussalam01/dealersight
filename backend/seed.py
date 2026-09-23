"""Deterministic fictional dealership data.

Everything here is invented. Dealership, region, promotion, and model-group names
are fictional; sales and finance deals are simulated business records that Ring
cannot observe. The seeded history is stored as raw events labelled
`simulated_baseline` on permanently placed cameras, so it runs through exactly
the same correlation rules as live Ring events instead of being written by hand.

Counts are monotonic by construction: visits >= engagements >= probable
test-drive sessions >= sales >= finance deals.

The deterministic dataset was designed to contain specific dealership scenarios for demonstrating the analytics. DealerSight's metrics and alert logic still calculate and detect those scenarios rather than displaying hard-coded conclusions.

The fixed seed below was selected so the intended scenarios (sales dip with
steady test drives, test-drive growth without sales, promotion lift) are present
in the generated data.
"""

import random
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

SEED = 8  # selected so the intended demo scenarios exist in the generated data
MODEL_GROUPS = ["Compact SUV", "Midsize Sedan", "Pickup", "EV Hatchback"]
PROMOTION = {"code": "fall_apr", "name": "Fall 0.9% APR Event (fictional)"}
OPENS, CLOSES = 9, 19  # business hours used for seeded activity

# rate_a / rate_b = period A (older 7 days) and period B (most recent 7 days)
DEALERS = [
    {"code": "cedar_ridge", "name": "Cedar Ridge Motors", "region": "Northgate", "is_demo": True,
     "visits": 36, "engagement_rate": 0.52, "test_drive_rate": (0.34, 0.34), "sale_rate": (0.40, 0.39),
     "note": "demo dealership, steady"},
    {"code": "brookfield", "name": "Brookfield Auto Group", "region": "Northgate", "is_demo": False,
     "visits": 42, "engagement_rate": 0.50, "test_drive_rate": (0.33, 0.33), "sale_rate": (0.42, 0.31),
     "note": "probable test drives steady, sales fall"},
    {"code": "larkspur", "name": "Larkspur Motors", "region": "Southvale", "is_demo": False,
     "visits": 30, "engagement_rate": 0.54, "test_drive_rate": (0.30, 0.43), "sale_rate": (0.40, 0.28),
     "note": "probable test drives rise, sales stay flat"},
    {"code": "vantage_hills", "name": "Vantage Hills Auto", "region": "Southvale", "is_demo": False,
     "visits": 38, "engagement_rate": 0.51, "test_drive_rate": (0.32, 0.32), "sale_rate": (0.41, 0.40),
     "note": "steady comparison dealership"},
]
FINANCE_RATE = (0.55, 0.72)  # share of simulated sales financed: before vs during the promotion
MAX_PENETRATION = 0.85       # never show every sale as financed: some customers pay cash


def period_bounds(today):
    """Two equal 7-day periods: A (older) then B (most recent), both ending at midnight UTC today."""
    end_b = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
    return (end_b - timedelta(days=14), end_b - timedelta(days=7)), (end_b - timedelta(days=7), end_b)


def seed_all(conn, today=None, seed=SEED):
    today = today or datetime.now(timezone.utc).date()
    (start_a, end_a), (start_b, end_b) = period_bounds(today)
    rng = random.Random(seed)

    with conn.transaction():
        # Derived rows reference raw events, so clear them first; correlation.rebuild recreates them.
        conn.execute("DELETE FROM test_drive_candidate")
        conn.execute("DELETE FROM derived_event")
        conn.execute("DELETE FROM finance_deal")
        conn.execute("DELETE FROM sale")
        conn.execute("DELETE FROM raw_event WHERE source = 'simulated_baseline'")
        conn.execute("DELETE FROM promotion")

        promotion_id = conn.execute(
            "INSERT INTO promotion (code, name, starts_at, ends_at) VALUES (%s, %s, %s, %s) RETURNING id",
            (PROMOTION["code"], PROMOTION["name"], start_b, end_b),
        ).fetchone()["id"]

        for spec in DEALERS:
            dealer_id = _dealer(conn, spec)
            devices = _cameras(conn, dealer_id, spec["code"], start_a - timedelta(days=1))
            for index, (start, rates) in enumerate([(start_a, 0), (start_b, 1)]):
                for day in range(7):
                    date = start + timedelta(days=day)
                    counts = _day_counts(spec, rates, rng)
                    _day_events(conn, devices, date, counts, rng)
                    _day_sales(conn, dealer_id, date, counts["sales"],
                               FINANCE_RATE[index], promotion_id if index == 1 else None, rng)

        _cap_penetration(conn, [start_a, start_b])

        # Live Ring events land on the demo dealership.
        conn.execute(
            """UPDATE device SET dealer_id = (SELECT id FROM dealer WHERE is_demo)
               WHERE mode = 'demo' AND ring_device_id NOT LIKE 'replay.%%'""")
    return {"dealers": len(DEALERS), "period_a": [start_a, end_a], "period_b": [start_b, end_b]}


def _cap_penetration(conn, period_starts):
    """Drop the newest finance deals where a dealership's penetration exceeds the cap."""
    for start in period_starts:
        end = start + timedelta(days=7)
        for row in conn.execute(
            """SELECT dealer_id, count(*) AS sales FROM sale WHERE occurred_at >= %s AND occurred_at < %s
               GROUP BY dealer_id""", (start, end)).fetchall():
            allowed = int(row["sales"] * MAX_PENETRATION)
            conn.execute(
                """DELETE FROM finance_deal WHERE id IN (
                       SELECT id FROM finance_deal WHERE dealer_id = %s AND occurred_at >= %s AND occurred_at < %s
                       ORDER BY id DESC LIMIT GREATEST(0, (
                           SELECT count(*) FROM finance_deal WHERE dealer_id = %s AND occurred_at >= %s AND occurred_at < %s
                       ) - %s))""",
                (row["dealer_id"], start, end + timedelta(hours=3), row["dealer_id"], start, end + timedelta(hours=3), allowed),
            )


def _dealer(conn, spec):
    return conn.execute(
        """INSERT INTO dealer (code, name, region, is_demo) VALUES (%(code)s, %(name)s, %(region)s, %(is_demo)s)
           ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, region = EXCLUDED.region, is_demo = EXCLUDED.is_demo
           RETURNING id""",
        spec,
    ).fetchone()["id"]


def _cameras(conn, dealer_id, dealer_code, placed_at):
    """One permanently placed camera per position, as a production deployment would have."""
    devices = {}
    for position in ("entrance", "display_area", "lot_departure", "lot_return"):
        ring_id = f"seed.device.{dealer_code}.{position}"
        device_id = conn.execute(
            """INSERT INTO device (ring_device_id, display_name, mode, dealer_id)
               VALUES (%s, %s, 'production', %s)
               ON CONFLICT (ring_device_id) DO UPDATE SET dealer_id = EXCLUDED.dealer_id RETURNING id""",
            (ring_id, f"{position.replace('_', ' ').title()} camera", dealer_id),
        ).fetchone()["id"]
        position_id = conn.execute("SELECT id FROM camera_position WHERE code = %s", (position,)).fetchone()["id"]
        if not conn.execute("SELECT 1 FROM device_assignment WHERE device_id = %s", (device_id,)).fetchone():
            conn.execute(
                """INSERT INTO device_assignment (device_id, camera_position_id, valid_from, zone_source)
                   VALUES (%s, %s, %s, 'device_configuration')""",
                (device_id, position_id, placed_at),
            )
        devices[position] = (device_id, position_id)
    return devices


def _day_counts(spec, period, rng):
    visits = max(1, round(spec["visits"] * rng.uniform(0.92, 1.08)))
    engagements = max(1, round(visits * spec["engagement_rate"] * rng.uniform(0.95, 1.05)))
    test_drives = max(1, round(engagements * spec["test_drive_rate"][period] * rng.uniform(0.95, 1.05)))
    sales = round(test_drives * spec["sale_rate"][period] * rng.uniform(0.9, 1.1))
    return {"visits": visits, "engagements": min(engagements, visits),
            "test_drives": min(test_drives, engagements), "sales": min(sales, test_drives)}


def _day_events(conn, devices, date, counts, rng):
    rows = []
    for index in range(counts["visits"]):  # entrance activity, always more than the cooldown apart
        start = date + timedelta(hours=OPENS, seconds=index * 480 + rng.randint(0, 200))
        rows.append((devices["entrance"], start, rng.randint(15, 45)))
    for index in range(counts["engagements"]):  # display-area dwell above the engagement threshold
        start = date + timedelta(hours=OPENS, seconds=index * 900 + rng.randint(0, 300))
        rows.append((devices["display_area"], start, rng.randint(25, 70)))
    for _ in range(max(1, counts["engagements"] // 5)):  # short looks that do not reach the threshold
        start = date + timedelta(hours=OPENS, minutes=rng.randint(0, 9 * 60))
        rows.append((devices["display_area"], start, rng.randint(8, 18)))
    for index in range(counts["test_drives"]):  # departure and a return inside the production window
        departure = date + timedelta(hours=OPENS, seconds=index * 1500 + rng.randint(0, 400))
        rows.append((devices["lot_departure"], departure, 25))
        rows.append((devices["lot_return"], departure + timedelta(minutes=rng.randint(12, 48)), 25))
    if rng.random() < 0.5:  # a departure that never returns, so expiry is visible in the data
        rows.append((devices["lot_departure"], date + timedelta(hours=CLOSES - 1, minutes=rng.randint(0, 50)), 25))

    conn.cursor().executemany(
        """INSERT INTO raw_event (ring_event_id, device_id, ring_event_type, started_at, ended_at, received_at,
                                  camera_position_id, zone_source, accepted, source, payload)
           VALUES (%s, %s, 'motion', %s, %s, %s, %s, 'device_configuration', true, 'simulated_baseline', %s)
           ON CONFLICT (ring_event_id) DO NOTHING""",
        [(f"seed-{device_id}-{int(start.timestamp())}", device_id, start, start + timedelta(seconds=seconds),
          start + timedelta(seconds=seconds + 30), position_id,
          Jsonb({"simulated_baseline": True, "note": "Not a Ring event: seeded demo history in Ring event shape"}))
         for (device_id, position_id), start, seconds in rows],
    )


def _day_sales(conn, dealer_id, date, sales, finance_rate, promotion_id, rng):
    for index in range(sales):
        occurred = date + timedelta(hours=OPENS, seconds=index * 2400 + rng.randint(0, 900))
        sale_id = conn.execute(
            "INSERT INTO sale (dealer_id, occurred_at, model_group) VALUES (%s, %s, %s) RETURNING id",
            (dealer_id, occurred, rng.choice(MODEL_GROUPS)),
        ).fetchone()["id"]
        if rng.random() < finance_rate:
            conn.execute(
                """INSERT INTO finance_deal (sale_id, dealer_id, occurred_at, promotion_id) VALUES (%s, %s, %s, %s)""",
                (sale_id, dealer_id, occurred + timedelta(minutes=rng.randint(20, 90)), promotion_id),
            )
