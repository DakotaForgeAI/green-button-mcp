"""Each MCP tool, called the way the server calls it."""

import asyncio
import logging
import os
from datetime import datetime

import generate_sample as gen
import pytest

from green_button_mcp import server

CHICAGO = gen.TZ


def expected_kwh(first: str, last: str) -> float:
    lo, hi = datetime.fromisoformat(first).date(), datetime.fromisoformat(last).date()
    return sum(
        wh for ts, _, wh in gen.generate_readings() if lo <= datetime.fromtimestamp(ts, CHICAGO).date() <= hi
    ) / 1000


def test_tools_registered_read_only():
    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    assert names == {
        "summarize_file", "daily_usage", "hourly_profile", "peak_days", "compare_periods", "estimate_cost",
    }
    for t in tools:
        assert t.annotations.readOnlyHint is True
        assert t.annotations.destructiveHint is False
        assert t.annotations.openWorldHint is False


def test_call_tool_through_mcp(sample_path):
    result = asyncio.run(server.mcp.call_tool("summarize_file", {"path": sample_path}))
    assert "2680" in str(result)


def test_summarize_file(sample_path):
    s = server.summarize_file(sample_path)
    assert s["first_day"] == "2025-10-20" and s["last_day"] == "2025-11-16"
    assert s["days_covered"] == 28
    assert s["reading_count"] == 2680
    assert s["interval_minutes"] == 15
    assert s["total_kwh"] == pytest.approx(expected_kwh("2025-10-20", "2025-11-16"), abs=1e-3)
    assert s["missing_intervals"] == 12
    assert s["gaps"][0]["start"] == "2025-10-29T13:00:00-05:00"
    assert s["duplicate_readings_removed"] == 4
    assert "daylight saving" in s["time_zone"]


def test_daily_usage(sample_path):
    r = server.daily_usage(sample_path, "2025-10-28", "2025-10-30")
    assert [d["date"] for d in r["days"]] == ["2025-10-28", "2025-10-29", "2025-10-30"]
    assert r["total_kwh"] == pytest.approx(expected_kwh("2025-10-28", "2025-10-30"), abs=1e-3)
    oct29 = r["days"][1]
    assert oct29["missing_intervals"] == 12 and not oct29["complete"]
    assert r["incomplete_days"] == ["2025-10-29"]


def test_daily_usage_defaults_and_empty_days(sample_path):
    r = server.daily_usage(sample_path)
    assert len(r["days"]) == 28
    out = server.daily_usage(sample_path, "2025-12-01", "2025-12-02")
    assert all(d["kwh"] is None and d["missing_intervals"] == 96 for d in out["days"])


def test_hourly_profile(sample_path):
    r = server.hourly_profile(sample_path)
    assert len(r["hours"]) == 24
    assert r["peak_hour"] in {"17:00-18:00", "18:00-19:00", "19:00-20:00"}
    assert r["lowest_hour"] in {f"{h:02d}:00-{h + 1:02d}:00" for h in range(0, 5)}
    assert r["partial_hours_excluded"] == 0  # the gap is exactly 3 whole hours, so it drops out
    assert r["hours"][13]["hours_averaged"] == 27  # 28 days minus the gap day
    assert r["hours"][1]["hours_averaged"] == 29  # fall-back day has two 1 a.m. hours


def test_peak_days(sample_path):
    r = server.peak_days(sample_path, 3)
    kwh = [d["kwh"] for d in r["peak_days"]]
    assert len(kwh) == 3 and kwh == sorted(kwh, reverse=True)
    all_days = server.daily_usage(sample_path)["days"]
    assert kwh[0] == max(d["kwh"] for d in all_days)
    with pytest.raises(ValueError):
        server.peak_days(sample_path, 0)


def test_compare_periods(sample_path):
    r = server.compare_periods(sample_path, "2025-10-20", "2025-10-26", "2025-11-10", "2025-11-16")
    a, b = expected_kwh("2025-10-20", "2025-10-26"), expected_kwh("2025-11-10", "2025-11-16")
    assert r["period_a"]["kwh"] == pytest.approx(a, abs=1e-3)
    assert r["period_b"]["kwh"] == pytest.approx(b, abs=1e-3)
    assert r["pct_change_total"] == pytest.approx((b - a) / a * 100, abs=0.06)
    assert r["period_a"]["complete"] and r["period_b"]["complete"]


def test_compare_periods_flags_missing(sample_path):
    r = server.compare_periods(sample_path, "2025-10-27", "2025-11-02", "2025-11-03", "2025-11-09")
    assert r["period_a"]["missing_intervals"] == 12
    assert any("missing readings" in n for n in r["notes"])
    with pytest.raises(ValueError):
        server.compare_periods(sample_path, "2025-10-30", "2025-10-27", "2025-11-03", "2025-11-09")


def test_estimate_cost(sample_path):
    r = server.estimate_cost(sample_path, "2025-11-03", "2025-11-09", 0.12, 10.0)
    kwh = expected_kwh("2025-11-03", "2025-11-09")
    assert r["is_estimate"] is True and "ESTIMATE" in r["label"]
    assert r["energy_charge"] == pytest.approx(kwh * 0.12, abs=0.01)
    assert r["fixed_charge_prorated"] == pytest.approx(10.0 * 7 / (365.25 / 12), abs=0.01)
    assert r["estimated_total"] == pytest.approx(r["energy_charge"] + r["fixed_charge_prorated"], abs=0.011)
    with pytest.raises(ValueError):
        server.estimate_cost(sample_path, "2025-11-03", "2025-11-09", -0.1)


def test_bad_date_message(sample_path):
    with pytest.raises(ValueError, match="date like"):
        server.daily_usage(sample_path, "last tuesday")


def test_tools_never_write_or_log_values(sample_path, tmp_path, caplog):
    import shutil

    path = str(shutil.copy(sample_path, tmp_path / "usage.xml"))
    os.chmod(path, 0o444)  # read-only file still works

    def snapshot():
        return {p.name: p.stat().st_mtime_ns for p in tmp_path.iterdir()}

    before = snapshot()
    caplog.set_level(logging.DEBUG)
    total = server.summarize_file(path)["total_kwh"]
    server.daily_usage(path)
    server.hourly_profile(path)
    server.peak_days(path)
    server.compare_periods(path, "2025-10-20", "2025-10-26", "2025-11-10", "2025-11-16")
    server.estimate_cost(path, "2025-11-03", "2025-11-09", 0.12)
    assert snapshot() == before  # nothing written, nothing created
    assert str(total) not in caplog.text
