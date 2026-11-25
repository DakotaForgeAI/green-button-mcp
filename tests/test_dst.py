from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import generate_sample as gen
import pytest

from green_button_mcp import analysis, espi
from green_button_mcp.espi import DstRule, LocalTime

CHICAGO = ZoneInfo("America/Chicago")


def us_central() -> LocalTime:
    return LocalTime(-21600, 3600, DstRule.decode("360E2000"), DstRule.decode("B40E2000"))


def test_decode_us_rules():
    start = DstRule.decode("360E2000")
    end = DstRule.decode("B40E2000")
    assert (start.month, start.operator, start.day_of_week, start.hour) == (3, 3, 7, 2)  # 2nd Sunday
    assert (end.month, end.operator, end.day_of_week, end.hour) == (11, 2, 7, 2)  # 1st Sunday
    assert start.wall_time(2026) == datetime(2026, 3, 8, 2)
    assert end.wall_time(2025) == datetime(2025, 11, 2, 2)


def test_disabled_and_invalid_rules():
    assert DstRule.decode("FFFFFFFF") is None
    assert DstRule.decode("not-hex") is None
    assert DstRule.decode(None) is None


def _rule(month, op, dom, dow, hour) -> str:
    return f"{(month << 28) | (op << 25) | (dom << 20) | (dow << 17) | (hour << 12):08X}"


def test_last_weekday_and_on_or_after_operators():
    last_sun_march = DstRule.decode(_rule(3, 7, 0, 7, 1))
    assert last_sun_march.wall_time(2026) == datetime(2026, 3, 29, 1)
    sun_on_or_after_8 = DstRule.decode(_rule(3, 1, 8, 7, 2))
    assert sun_on_or_after_8.wall_time(2026) == datetime(2026, 3, 8, 2)
    fixed_day = DstRule.decode(_rule(4, 0, 15, 0, 0))
    assert fixed_day.wall_time(2026) == datetime(2026, 4, 15)


def test_matches_zoneinfo_every_hour_2024_to_2027():
    lt = us_central()
    ts = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())
    end = int(datetime(2028, 1, 1, tzinfo=timezone.utc).timestamp())
    while ts < end:
        expected = datetime.fromtimestamp(ts, CHICAGO).utcoffset().total_seconds()
        assert lt.utc_offset(ts) == expected, datetime.fromtimestamp(ts, timezone.utc)
        ts += 900


@pytest.mark.parametrize(
    "day,hours", [(date(2026, 3, 8), 23), (date(2025, 11, 2), 25), (date(2025, 11, 3), 24)]
)
def test_day_lengths(day, hours):
    assert us_central().day_seconds(day) == hours * 3600


def test_spring_forward_day(write_xml):
    readings = gen.generate_readings(start=date(2026, 3, 7), days=3, gap=None)
    d = espi.load(write_xml(gen.to_xml(readings, duplicate_day=None)))
    days = {r["date"]: r for r in analysis.daily_usage(d)["days"]}
    mar8 = days["2026-03-08"]
    assert mar8["hours_in_day"] == 23
    assert mar8["expected_intervals"] == mar8["intervals_with_data"] == 92
    assert mar8["complete"]
    assert days["2026-03-07"]["intervals_with_data"] == 96
    # 2 a.m. does not exist that day
    hp = analysis.hourly_profile(d, "2026-03-08", "2026-03-08")
    assert hp["hours"][2]["hours_averaged"] == 0
    assert hp["hours"][3]["hours_averaged"] == 1


def test_fall_back_day_in_sample(sample_path):
    d = espi.load(sample_path)
    nov2 = next(r for r in analysis.daily_usage(d)["days"] if r["date"] == "2025-11-02")
    assert nov2["hours_in_day"] == 25
    assert nov2["expected_intervals"] == nov2["intervals_with_data"] == 100
    assert nov2["complete"]
    # 1 a.m. happens twice: counted as two separate clock hours
    hp = analysis.hourly_profile(d, "2025-11-02", "2025-11-02")
    assert hp["hours"][1]["hours_averaged"] == 2
    assert hp["hours"][0]["hours_averaged"] == 1


def test_day_assignment_matches_zoneinfo(sample_path):
    d = espi.load(sample_path)
    for r in d.readings[::37]:
        assert d.local_time.local_date(r.start) == datetime.fromtimestamp(r.start, CHICAGO).date()
        assert d.local_time.to_local(r.start) == datetime.fromtimestamp(r.start, CHICAGO)
    assert d.local_time.midnight_utc(date(2025, 11, 3)) == int(
        datetime(2025, 11, 3, tzinfo=CHICAGO).timestamp()
    )
    assert d.local_time.midnight_utc(date(2025, 11, 2)) == int(
        datetime(2025, 11, 2, tzinfo=CHICAGO).timestamp()
    )
