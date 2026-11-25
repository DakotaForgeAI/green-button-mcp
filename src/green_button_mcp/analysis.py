"""Usage calculations behind each MCP tool. Pure functions over UsageData."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from .espi import Reading, UsageData

MAX_GAPS_LISTED = 25
DAYS_PER_MONTH = 365.25 / 12


def _r(x: float, places: int = 3) -> float:
    return round(x + 0.0, places)


def parse_day(value: str | None, name: str) -> date | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        raise ValueError(f"{name} must be a date like 2025-10-31 (got {value!r}).") from None


def _range(data: UsageData, start: date | None, end: date | None) -> tuple[date, date]:
    lt = data.local_time
    first = lt.local_date(data.readings[0].start)
    last = lt.local_date(data.readings[-1].end - 1)
    s = start or first
    e = end or last
    if s > e:
        raise ValueError(f"Start date {s} is after end date {e}.")
    return s, e


def _in_range(data: UsageData, s: date, e: date) -> list[Reading]:
    lt = data.local_time
    return [r for r in data.readings if s <= lt.local_date(r.start) <= e]


def _coverage(data: UsageData, s: date, e: date, readings: list[Reading]) -> dict:
    lt = data.local_time
    expected_seconds = lt.midnight_utc(e + timedelta(days=1)) - lt.midnight_utc(s)
    covered = sum(r.duration for r in readings)
    missing = max(0, round((expected_seconds - covered) / data.interval_seconds))
    return {
        "expected_intervals": round(expected_seconds / data.interval_seconds),
        "intervals_with_data": len(readings),
        "missing_intervals": missing,
        "complete": missing == 0,
    }


def _iso(data: UsageData, ts: int) -> str:
    return data.local_time.to_local(ts).isoformat()


# --------------------------------------------------------------------------


def summarize(data: UsageData) -> dict:
    lt = data.local_time
    first, last = _range(data, None, None)
    total = sum(r.kwh for r in data.readings)
    gaps = [
        {"start": _iso(data, g.start), "end": _iso(data, g.end), "missing_intervals": g.missing_intervals}
        for g in data.gaps[:MAX_GAPS_LISTED]
    ]
    result = {
        "file": data.path,
        "time_zone": lt.describe(),
        "first_reading_start": _iso(data, data.readings[0].start),
        "last_reading_end": _iso(data, data.readings[-1].end),
        "first_day": first.isoformat(),
        "last_day": last.isoformat(),
        "days_covered": (last - first).days + 1,
        "reading_count": len(data.readings),
        "interval_minutes": data.interval_seconds / 60,
        "total_kwh": _r(total),
        "missing_intervals": data.missing_intervals,
        "gap_count": len(data.gaps),
        "gaps": gaps,
        "duplicate_readings_removed": data.duplicates_removed,
        "conflicting_duplicates": data.conflicting_duplicates,
        "series_in_file": [
            {
                "direction": s.reading_type.flow_name,
                "unit": s.reading_type.unit_name,
                "power_of_ten_multiplier": s.reading_type.power_of_ten,
                "readings": s.raw_count,
                "used_for_totals": s.used,
            }
            for s in data.series
        ],
        "warnings": data.warnings,
    }
    if len(data.gaps) > MAX_GAPS_LISTED:
        result["gaps_not_listed"] = len(data.gaps) - MAX_GAPS_LISTED
    if len(data.interval_lengths_seen) > 1:
        result["interval_lengths_seen_minutes"] = [x / 60 for x in data.interval_lengths_seen]
    return result


def _daily_rows(data: UsageData, s: date, e: date) -> list[dict]:
    lt = data.local_time
    per_day: dict[date, list[Reading]] = defaultdict(list)
    for r in _in_range(data, s, e):
        per_day[lt.local_date(r.start)].append(r)
    rows = []
    d = s
    while d <= e:
        rs = per_day.get(d, [])
        cov = _coverage(data, d, d, rs)
        rows.append(
            {
                "date": d.isoformat(),
                "weekday": d.strftime("%A"),
                "kwh": _r(sum(r.kwh for r in rs)) if rs else None,
                "hours_in_day": lt.day_seconds(d) / 3600,
                **cov,
            }
        )
        d += timedelta(days=1)
    return rows


def daily_usage(data: UsageData, start: str | None = None, end: str | None = None) -> dict:
    s, e = _range(data, parse_day(start, "start"), parse_day(end, "end"))
    rows = _daily_rows(data, s, e)
    with_data = [r for r in rows if r["kwh"] is not None]
    return {
        "time_zone": data.local_time.describe(),
        "start": s.isoformat(),
        "end": e.isoformat(),
        "total_kwh": _r(sum(r["kwh"] for r in with_data)),
        "days": rows,
        "incomplete_days": [r["date"] for r in rows if not r["complete"]],
        "note": "Days are local calendar days. DST change days have 23 or 25 hours. "
        "kwh is null for days with no readings; incomplete days are undercounted, not estimated.",
    }


def hourly_profile(data: UsageData, start: str | None = None, end: str | None = None) -> dict:
    s, e = _range(data, parse_day(start, "start"), parse_day(end, "end"))
    lt = data.local_time
    # Bucket by actual clock hour (local hour + UTC offset), so the repeated
    # 1 a.m. hour on a fall-back day is two separate hours, not one doubled hour.
    buckets: dict[tuple, list[Reading]] = defaultdict(list)
    for r in _in_range(data, s, e):
        local = lt.to_local(r.start)
        buckets[(local.date(), local.hour, local.utcoffset())].append(r)
    sums: dict[int, float] = defaultdict(float)
    counts: dict[int, int] = defaultdict(int)
    partial = 0
    for (_, hour, _), rs in buckets.items():
        if sum(r.duration for r in rs) < 3600:
            partial += 1
            continue
        sums[hour] += sum(r.kwh for r in rs)
        counts[hour] += 1
    hours = [
        {
            "hour": h,
            "label": f"{h:02d}:00-{(h + 1) % 24:02d}:00",
            "avg_kwh": _r(sums[h] / counts[h]) if counts[h] else None,
            "hours_averaged": counts[h],
        }
        for h in range(24)
    ]
    valid = [x for x in hours if x["avg_kwh"] is not None]
    return {
        "time_zone": lt.describe(),
        "start": s.isoformat(),
        "end": e.isoformat(),
        "hours": hours,
        "peak_hour": max(valid, key=lambda x: x["avg_kwh"])["label"] if valid else None,
        "lowest_hour": min(valid, key=lambda x: x["avg_kwh"])["label"] if valid else None,
        "partial_hours_excluded": partial,
        "note": "Average kWh used in each local clock hour. Hours with missing readings are left out of the average.",
    }


def peak_days(data: UsageData, n: int = 5) -> dict:
    if not isinstance(n, int) or n < 1:
        raise ValueError("n must be a positive whole number.")
    n = min(n, 366)
    s, e = _range(data, None, None)
    rows = [r for r in _daily_rows(data, s, e) if r["kwh"] is not None]
    rows.sort(key=lambda r: r["kwh"], reverse=True)
    complete = [r["kwh"] for r in rows if r["complete"]]
    return {
        "time_zone": data.local_time.describe(),
        "peak_days": rows[:n],
        "average_complete_day_kwh": _r(sum(complete) / len(complete)) if complete else None,
        "note": "Ranked by local-day kWh. Days marked complete=false are missing readings, so their totals are low.",
    }


def _period(data: UsageData, start: str, end: str, label: str) -> dict:
    s = parse_day(start, f"{label}_start")
    e = parse_day(end, f"{label}_end")
    if s is None or e is None:
        raise ValueError(f"Both {label}_start and {label}_end are required.")
    if s > e:
        raise ValueError(f"{label}_start {s} is after {label}_end {e}.")
    rs = _in_range(data, s, e)
    days = (e - s).days + 1
    kwh = sum(r.kwh for r in rs)
    return {
        "start": s.isoformat(),
        "end": e.isoformat(),
        "days": days,
        "kwh": _r(kwh),
        "avg_kwh_per_day": _r(kwh / days),
        **_coverage(data, s, e, rs),
    }


def _pct(a: float, b: float):
    return None if a == 0 else _r((b - a) / a * 100, 1)


def compare_periods(data: UsageData, a_start: str, a_end: str, b_start: str, b_end: str) -> dict:
    a = _period(data, a_start, a_end, "a")
    b = _period(data, b_start, b_end, "b")
    notes = ["Percent change is (B - A) / A."]
    if a["days"] != b["days"]:
        notes.append("The periods have different lengths; compare avg_kwh_per_day too.")
    if not (a["complete"] and b["complete"]):
        notes.append("At least one period is missing readings, so its total is undercounted.")
    return {
        "time_zone": data.local_time.describe(),
        "period_a": a,
        "period_b": b,
        "kwh_change": _r(b["kwh"] - a["kwh"]),
        "pct_change_total": _pct(a["kwh"], b["kwh"]),
        "pct_change_per_day": _pct(a["avg_kwh_per_day"], b["avg_kwh_per_day"]),
        "notes": notes,
    }


def estimate_cost(
    data: UsageData, start: str, end: str, rate_per_kwh: float, monthly_fixed: float = 0.0
) -> dict:
    for name, val in (("rate_per_kwh", rate_per_kwh), ("monthly_fixed", monthly_fixed)):
        if not isinstance(val, (int, float)) or isinstance(val, bool) or val < 0:
            raise ValueError(f"{name} must be a number zero or greater.")
    p = _period(data, start, end, "period")
    energy = p["kwh"] * rate_per_kwh
    months = p["days"] / DAYS_PER_MONTH
    fixed = monthly_fixed * months
    return {
        "is_estimate": True,
        "label": "ESTIMATE ONLY: based solely on the rates you supplied. This is not a bill.",
        "start": p["start"],
        "end": p["end"],
        "days": p["days"],
        "kwh": p["kwh"],
        "rate_per_kwh": rate_per_kwh,
        "energy_charge": _r(energy, 2),
        "monthly_fixed": monthly_fixed,
        "fixed_charge_prorated": _r(fixed, 2),
        "months_prorated": _r(months, 3),
        "estimated_total": _r(energy + fixed, 2),
        "missing_intervals": p["missing_intervals"],
        "excludes": [
            "taxes and franchise fees",
            "riders and fuel/energy cost adjustments",
            "tiered, seasonal, time-of-use, or demand charges",
            "credits, discounts, and net-metering",
        ],
        "notes": [
            "Uses no utility rate tables. Fixed charge is prorated at days / 30.44.",
        ]
        + (["The period is missing readings, so this estimate is low."] if not p["complete"] else []),
    }
