"""MCP server (stdio) for Green Button electricity usage files.

All tools are read-only. The server reads only the file path it is given,
makes no network calls, writes nothing, and never logs usage values.
Nothing may be printed to stdout: stdio is the MCP transport.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import analysis
from .espi import FileCache

mcp = FastMCP(
    "green-button",
    instructions=(
        "Answers questions about electricity usage from a Green Button 'Download My Data' "
        "XML (or .zip) file. Every tool takes the file path. Start with summarize_file. "
        "Always mention missing intervals or incomplete days when they affect an answer. "
        "estimate_cost is an estimate from user-supplied rates only, never a bill."
    ),
    log_level="WARNING",
)

_cache = FileCache()
_RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


@mcp.tool(annotations=_RO)
def summarize_file(path: str) -> dict:
    """Overview of a Green Button file: date range, number of readings, interval
    length, total kWh, time zone, and any gaps (missing intervals) or duplicates.

    Args:
        path: Path to the Green Button XML or .zip file on this computer.
    """
    return analysis.summarize(_cache.get(path))


@mcp.tool(annotations=_RO)
def daily_usage(path: str, start: str | None = None, end: str | None = None) -> dict:
    """kWh per local calendar day (DST-aware), with how complete each day is.

    Args:
        path: Path to the Green Button XML or .zip file.
        start: First day, YYYY-MM-DD (inclusive). Defaults to the file's first day.
        end: Last day, YYYY-MM-DD (inclusive). Defaults to the file's last day.
    """
    return analysis.daily_usage(_cache.get(path), start, end)


@mcp.tool(annotations=_RO)
def hourly_profile(path: str, start: str | None = None, end: str | None = None) -> dict:
    """Average kWh for each hour of the day (local time), e.g. to find when usage peaks.

    Args:
        path: Path to the Green Button XML or .zip file.
        start: First day, YYYY-MM-DD (inclusive). Optional.
        end: Last day, YYYY-MM-DD (inclusive). Optional.
    """
    return analysis.hourly_profile(_cache.get(path), start, end)


@mcp.tool(annotations=_RO)
def peak_days(path: str, n: int = 5) -> dict:
    """The n highest-usage local days, highest first.

    Args:
        path: Path to the Green Button XML or .zip file.
        n: How many days to return (default 5).
    """
    return analysis.peak_days(_cache.get(path), n)


@mcp.tool(annotations=_RO)
def compare_periods(path: str, a_start: str, a_end: str, b_start: str, b_end: str) -> dict:
    """Compare kWh between period A and period B (dates inclusive, YYYY-MM-DD),
    with totals, per-day averages, and percent change from A to B.

    Args:
        path: Path to the Green Button XML or .zip file.
        a_start: Period A first day.
        a_end: Period A last day.
        b_start: Period B first day.
        b_end: Period B last day.
    """
    return analysis.compare_periods(_cache.get(path), a_start, a_end, b_start, b_end)


@mcp.tool(annotations=_RO)
def estimate_cost(
    path: str, start: str, end: str, rate_per_kwh: float, monthly_fixed: float = 0.0
) -> dict:
    """ESTIMATE the cost of usage between two dates from rates the user supplies.
    Uses no utility rate tables and excludes taxes, riders, tiers, time-of-use and
    demand charges. Always present the result as an estimate, not a bill.

    Args:
        path: Path to the Green Button XML or .zip file.
        start: First day, YYYY-MM-DD (inclusive).
        end: Last day, YYYY-MM-DD (inclusive).
        rate_per_kwh: Price per kWh in dollars, as given by the user (e.g. 0.12).
        monthly_fixed: Fixed monthly customer charge in dollars, prorated by days (default 0).
    """
    return analysis.estimate_cost(_cache.get(path), start, end, rate_per_kwh, monthly_fixed)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
