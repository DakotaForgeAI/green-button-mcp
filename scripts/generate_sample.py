#!/usr/bin/env python3
"""Generate a SYNTHETIC Green Button (NAESB ESPI) XML file.

Every value comes from a seeded random load model. No real meter data is
used or needed. The default output is samples/synthetic_green_button.xml:
four weeks of 15-minute reads in US Central time that cross the November
DST change. The sample also includes one deliberate 3-hour gap and a few
duplicated readings, so the parser's gap and duplicate reporting can be
seen working.

Usage:
    python scripts/generate_sample.py                  # writes the default sample
    python scripts/generate_sample.py --start 2026-03-01 --days 14 -o out.xml
"""

from __future__ import annotations

import argparse
import hashlib
import math
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Chicago")  # South Dakota (east river) is US Central
TZ_OFFSET = -6 * 3600
DST_OFFSET = 3600
DST_START_RULE = "360E2000"  # 2nd Sunday in March, 02:00
DST_END_RULE = "B40E2000"  # 1st Sunday in November, 02:00

BASE = "https://datacustodian.example.com/DataCustodian/espi/1_1/resource"
UP = f"{BASE}/Subscription/1/UsagePoint/1"
MR = f"{UP}/MeterReading/1"
RT = f"{BASE}/ReadingType/1"
LTP = f"{BASE}/LocalTimeParameters/1"

DEFAULT_START = date(2025, 10, 20)
DEFAULT_DAYS = 28
DEFAULT_GAP = (datetime(2025, 10, 29, 13, 0), 12)  # local start, intervals missing
DEFAULT_DUP_DAY = date(2025, 11, 10)  # first 4 readings of this day repeated


def local_midnight_ts(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp())


def watt_hours(ts: int, interval: int, rng: random.Random) -> int:
    """Synthetic household load for one interval, in Wh."""
    local = datetime.fromtimestamp(ts, TZ)
    h = local.hour + local.minute / 60
    weekend = local.weekday() >= 5
    kw = 0.45  # fridge, standby, furnace fan
    kw += 0.9 * math.exp(-((h - (8.5 if weekend else 7.0)) ** 2) / 1.5)  # morning
    kw += 1.6 * math.exp(-((h - 18.5) ** 2) / 3.0)  # evening cooking, laundry, TV
    if weekend:
        kw += 0.5 * math.exp(-((h - 13) ** 2) / 8)
    day_of_year = local.timetuple().tm_yday
    kw *= 1 + 0.004 * max(0, day_of_year - 290)  # colder late in the season
    kw *= rng.uniform(0.85, 1.15)
    if rng.random() < 0.03:  # dryer / oven / water heater cycle
        kw += rng.uniform(1.5, 3.5)
    return max(1, round(kw * 1000 * interval / 3600))


def generate_readings(
    start: date = DEFAULT_START,
    days: int = DEFAULT_DAYS,
    seed: int = 42,
    interval: int = 900,
    gap: tuple[datetime, int] | None = DEFAULT_GAP,
) -> list[tuple[int, int, int]]:
    """Unique readings as (utc_start, duration_seconds, Wh)."""
    rng = random.Random(seed)
    t0 = local_midnight_ts(start)
    t1 = local_midnight_ts(start + timedelta(days=days))
    skip: set[int] = set()
    if gap:
        g0 = int(gap[0].replace(tzinfo=TZ).timestamp())
        skip = {g0 + i * interval for i in range(gap[1])}
    out = []
    for ts in range(t0, t1, interval):
        wh = watt_hours(ts, interval, rng)
        if ts not in skip:
            out.append((ts, interval, wh))
    return out


def _entry(title: str, links: list[tuple[str, str]], content: str) -> str:
    link_xml = "".join(f'<link rel="{r}" href="{escape(h)}"/>' for r, h in links)
    return (
        f"<entry><id>urn:sha1:{hashlib.sha1(title.encode()).hexdigest()[:16]}</id>{link_xml}"
        f"<title>{escape(title)}</title><content>{content}</content>"
        "<published>2025-11-20T00:00:00Z</published><updated>2025-11-20T00:00:00Z</updated></entry>"
    )


def to_xml(
    readings: list[tuple[int, int, int]],
    power_of_ten: int = 0,
    interval: int = 900,
    duplicate_day: date | None = DEFAULT_DUP_DAY,
    include_local_time: bool = True,
) -> str:
    """Build an ESPI Atom feed. One IntervalBlock per local day."""
    blocks: dict[date, list[tuple[int, int, int]]] = {}
    for r in readings:
        blocks.setdefault(datetime.fromtimestamp(r[0], TZ).date(), []).append(r)

    if duplicate_day and duplicate_day in blocks:
        prev = duplicate_day - timedelta(days=1)
        if prev in blocks:
            blocks[prev] = blocks[prev] + blocks[duplicate_day][:4]

    scale = 10 ** power_of_ten

    def ir(ts: int, dur: int, wh: int) -> str:
        value = round(wh / scale)
        return (
            f"<IntervalReading><timePeriod><duration>{dur}</duration><start>{ts}</start>"
            f"</timePeriod><value>{value}</value></IntervalReading>"
        )

    entries = []
    up_links = [("self", UP), ("related", f"{UP}/MeterReading")]
    if include_local_time:
        up_links.append(("related", LTP))
    entries.append(
        _entry(
            "Synthetic Home",
            up_links,
            '<UsagePoint xmlns="http://naesb.org/espi"><ServiceCategory><kind>0</kind>'
            "</ServiceCategory></UsagePoint>",
        )
    )
    if include_local_time:
        entries.append(
            _entry(
                "Local Time",
                [("self", LTP)],
                '<LocalTimeParameters xmlns="http://naesb.org/espi">'
                f"<dstEndRule>{DST_END_RULE}</dstEndRule><dstOffset>{DST_OFFSET}</dstOffset>"
                f"<dstStartRule>{DST_START_RULE}</dstStartRule><tzOffset>{TZ_OFFSET}</tzOffset>"
                "</LocalTimeParameters>",
            )
        )
    entries.append(
        _entry(
            "Electricity",
            [("self", MR), ("related", f"{MR}/IntervalBlock"), ("related", RT)],
            '<MeterReading xmlns="http://naesb.org/espi"/>',
        )
    )
    entries.append(
        _entry(
            "Energy Delivered (kWh)",
            [("self", RT)],
            '<ReadingType xmlns="http://naesb.org/espi">'
            "<accumulationBehaviour>4</accumulationBehaviour><commodity>1</commodity>"
            "<currency>840</currency><dataQualifier>12</dataQualifier><flowDirection>1</flowDirection>"
            f"<intervalLength>{interval}</intervalLength><kind>12</kind><phase>769</phase>"
            f"<powerOfTenMultiplier>{power_of_ten}</powerOfTenMultiplier>"
            "<timeAttribute>0</timeAttribute><uom>72</uom></ReadingType>",
        )
    )
    for n, (day, rs) in enumerate(sorted(blocks.items()), start=1):
        start = local_midnight_ts(day)
        dur = local_midnight_ts(day + timedelta(days=1)) - start
        body = "".join(ir(*r) for r in rs)
        entries.append(
            _entry(
                f"Interval Block {day.isoformat()}",
                [("self", f"{MR}/IntervalBlock/{n}"), ("up", f"{MR}/IntervalBlock")],
                f'<IntervalBlock xmlns="http://naesb.org/espi"><interval><duration>{dur}</duration>'
                f"<start>{start}</start></interval>{body}</IntervalBlock>",
            )
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!-- SYNTHETIC DATA generated by scripts/generate_sample.py. Not from any real meter. -->\n"
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        "<id>urn:uuid:synthetic-green-button-sample</id>"
        "<title>Synthetic Green Button Usage Data</title>"
        "<updated>2025-11-20T00:00:00Z</updated>\n"
        + "\n".join(entries)
        + "\n</feed>\n"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--no-gap", action="store_true", help="don't remove readings to make a gap")
    ap.add_argument("--no-duplicates", action="store_true", help="don't repeat any readings")
    ap.add_argument(
        "-o", "--output", type=Path,
        default=Path(__file__).resolve().parent.parent / "samples" / "synthetic_green_button.xml",
    )
    args = ap.parse_args()
    defaults = args.start == DEFAULT_START and args.days == DEFAULT_DAYS
    gap = None if args.no_gap or not defaults else DEFAULT_GAP
    dup = None if args.no_duplicates else (DEFAULT_DUP_DAY if defaults else args.start + timedelta(days=1))
    readings = generate_readings(args.start, args.days, args.seed, gap=gap)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(to_xml(readings, duplicate_day=dup), encoding="utf-8")
    print(f"Wrote {args.output} ({len(readings)} synthetic readings)")


if __name__ == "__main__":
    main()
