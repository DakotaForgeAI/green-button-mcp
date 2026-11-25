"""Parser for Green Button "Download My Data" files (NAESB ESPI XML).

This module only reads. It never writes, uploads, or logs usage values.

What it handles:

* Atom feeds with ``UsagePoint`` / ``MeterReading`` / ``ReadingType`` /
  ``IntervalBlock`` / ``LocalTimeParameters`` entries, linked by Atom hrefs.
* Bare ESPI documents without the Atom wrapper.
* ``.zip`` downloads that contain a Green Button XML file.
* ``powerOfTenMultiplier`` and ``uom`` from ``ReadingType`` (energy in Wh is
  converted to kWh).
* Multiple interval blocks, duplicate readings (removed and counted), and gaps
  (reported, never filled in).
* Local time from ``LocalTimeParameters``, including the DST rules.
"""

from __future__ import annotations

import calendar
import io
import os
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from defusedxml import ElementTree as ET

MAX_FILE_BYTES = 256 * 1024 * 1024  # refuse anything larger than this

UOM_WH = 72
UOM_NAMES = {72: "Wh", 38: "W", 73: "VArh", 63: "VAr", 71: "VAh", 61: "VA", 169: "therm"}
FLOW_NAMES = {1: "delivered (from grid)", 4: "net", 19: "received (to grid)"}


# --------------------------------------------------------------------------
# Local time and DST
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DstRule:
    """One ESPI DST rule, decoded from its 32-bit hex form.

    Bit layout (ESPI ``DstRuleType``):
      0-11 seconds, 12-16 hour, 17-19 day of week (1 = Monday),
      20-24 day of month, 25-27 operator, 28-31 month.
    Operators: 0 = day of month, 1 = weekday on or after day of month,
    2-6 = first..fifth weekday of the month, 7 = last weekday of the month.
    """

    month: int
    operator: int
    day_of_month: int
    day_of_week: int
    hour: int
    seconds: int

    @classmethod
    def decode(cls, value: str | None) -> "DstRule | None":
        if not value:
            return None
        try:
            raw = int(value.strip(), 16)
        except ValueError:
            return None
        if raw == 0xFFFFFFFF:
            return None  # rule processing disabled
        rule = cls(
            month=(raw >> 28) & 0xF,
            operator=(raw >> 25) & 0x7,
            day_of_month=(raw >> 20) & 0x1F,
            day_of_week=(raw >> 17) & 0x7,
            hour=(raw >> 12) & 0x1F,
            seconds=raw & 0xFFF,
        )
        if not 1 <= rule.month <= 12 or rule.hour > 23:
            return None
        if rule.operator == 0 and rule.day_of_month == 0:
            return None
        if rule.operator >= 1 and rule.day_of_week == 0:
            return None
        return rule

    def wall_time(self, year: int) -> datetime:
        """Local wall-clock time of the transition in ``year`` (naive)."""
        weekday = self.day_of_week - 1  # Python: Monday = 0
        if self.operator == 0:
            day = self.day_of_month
        elif self.operator == 1:
            first = date(year, self.month, max(self.day_of_month, 1))
            day = first.day + (weekday - first.weekday()) % 7
        elif self.operator == 7:
            last_day = calendar.monthrange(year, self.month)[1]
            last = date(year, self.month, last_day)
            day = last_day - (last.weekday() - weekday) % 7
        else:
            nth = self.operator - 1
            first = date(year, self.month, 1)
            day = 1 + (weekday - first.weekday()) % 7 + 7 * (nth - 1)
            last_day = calendar.monthrange(year, self.month)[1]
            while day > last_day:  # "fifth" weekday that doesn't exist -> last
                day -= 7
        return datetime(year, self.month, day) + timedelta(hours=self.hour, seconds=self.seconds)


def _epoch(naive: datetime) -> int:
    return calendar.timegm(naive.timetuple())


class LocalTime:
    """Converts UTC epoch seconds to the file's local time.

    The DST start rule is read as local standard time and the end rule as
    local daylight time, which is how US rules are written (2:00 both ways).
    """

    def __init__(
        self,
        tz_offset: int = 0,
        dst_offset: int = 0,
        dst_start: DstRule | None = None,
        dst_end: DstRule | None = None,
        from_file: bool = True,
    ):
        self.tz_offset = tz_offset
        self.dst_offset = dst_offset
        self.dst_start = dst_start
        self.dst_end = dst_end
        self.from_file = from_file
        self.has_dst = bool(dst_offset and dst_start and dst_end)
        self._bounds = lru_cache(maxsize=64)(self._compute_bounds)

    def _compute_bounds(self, year: int) -> tuple[int, int]:
        start = _epoch(self.dst_start.wall_time(year)) - self.tz_offset
        end = _epoch(self.dst_end.wall_time(year)) - self.tz_offset - self.dst_offset
        return start, end

    def utc_offset(self, ts: int) -> int:
        if not self.has_dst:
            return self.tz_offset
        year = datetime.fromtimestamp(ts + self.tz_offset, timezone.utc).year
        start, end = self._bounds(year)
        in_dst = start <= ts < end if start < end else (ts >= start or ts < end)
        return self.tz_offset + (self.dst_offset if in_dst else 0)

    def to_local(self, ts: int) -> datetime:
        """Aware datetime in the file's local time."""
        offset = self.utc_offset(ts)
        return datetime.fromtimestamp(ts, timezone(timedelta(seconds=offset)))

    def local_date(self, ts: int) -> date:
        return datetime.fromtimestamp(ts + self.utc_offset(ts), timezone.utc).date()

    def midnight_utc(self, day: date) -> int:
        """UTC epoch of the first instant of local ``day``."""
        wall = _epoch(datetime(day.year, day.month, day.day))
        std = wall - self.tz_offset
        for candidate in (std - self.dst_offset, std):
            if candidate + self.utc_offset(candidate) == wall:
                return candidate
        return std  # midnight skipped by a DST jump: the day starts at the jump

    def day_seconds(self, day: date) -> int:
        """Length of local ``day``: 86400, or 82800/90000 on DST change days."""
        return self.midnight_utc(day + timedelta(days=1)) - self.midnight_utc(day)

    def describe(self) -> str:
        if not self.from_file:
            return "UTC (the file has no LocalTimeParameters, so times are shown in UTC)"
        sign = "+" if self.tz_offset >= 0 else "-"
        hours, rem = divmod(abs(self.tz_offset), 3600)
        text = f"UTC{sign}{hours:02d}:{rem // 60:02d} standard time"
        if self.has_dst:
            text += f", daylight saving +{self.dst_offset / 3600:g}h per the file's DST rules"
        else:
            text += ", no daylight saving"
        return text


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass
class ReadingTypeInfo:
    uom: int | None = None
    power_of_ten: int = 0
    interval_length: int | None = None
    flow_direction: int | None = None
    commodity: int | None = None
    href: str | None = None

    @property
    def unit_name(self) -> str:
        if self.uom is None:
            return "unknown unit"
        return UOM_NAMES.get(self.uom, f"uom code {self.uom}")

    @property
    def flow_name(self) -> str:
        if self.flow_direction is None:
            return "unspecified"
        return FLOW_NAMES.get(self.flow_direction, f"flowDirection {self.flow_direction}")


@dataclass(frozen=True)
class Reading:
    start: int  # UTC epoch seconds
    duration: int  # seconds
    kwh: float

    @property
    def end(self) -> int:
        return self.start + self.duration


@dataclass
class Gap:
    start: int
    end: int
    missing_intervals: int


@dataclass
class SeriesInfo:
    reading_type: ReadingTypeInfo
    raw_count: int
    used: bool = False


@dataclass
class UsageData:
    path: str
    readings: list[Reading]  # sorted, de-duplicated, kWh
    interval_seconds: int
    interval_lengths_seen: list[int]
    local_time: LocalTime
    gaps: list[Gap]
    duplicates_removed: int
    conflicting_duplicates: int
    overlapping_readings: int
    readings_without_value: int
    series: list[SeriesInfo]
    warnings: list[str] = field(default_factory=list)

    @property
    def missing_intervals(self) -> int:
        return sum(g.missing_intervals for g in self.gaps)


# --------------------------------------------------------------------------
# XML helpers
# --------------------------------------------------------------------------


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(el, name: str):
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _children(el, name: str):
    return [c for c in el if _local(c.tag) == name]


def _int(el, name: str, default=None):
    c = _child(el, name)
    if c is None or c.text is None or not c.text.strip():
        return default
    try:
        return int(c.text.strip())
    except ValueError:
        return default


def _text(el, name: str):
    c = _child(el, name)
    return c.text.strip() if c is not None and c.text else None


def _href_key(href: str | None) -> str | None:
    if not href:
        return None
    return urlparse(href.strip()).path.rstrip("/") or None


def _find_all(el, name: str):
    return [d for d in el.iter() if _local(d.tag) == name]


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def resolve_path(path: str) -> Path:
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Please give the path to a Green Button XML (or .zip) file.")
    p = Path(path.strip()).expanduser()
    if not p.is_file():
        raise ValueError(f"File not found: {p}")
    size = p.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ValueError(f"File is too large ({size} bytes); limit is {MAX_FILE_BYTES} bytes.")
    return p.resolve()


def _read_bytes(p: Path) -> bytes:
    with open(p, "rb") as fh:
        data = fh.read(MAX_FILE_BYTES + 1)
    if not zipfile.is_zipfile(io.BytesIO(data)):
        return data
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        xml_members = [i for i in zf.infolist() if i.filename.lower().endswith(".xml") and not i.is_dir()]
        if not xml_members:
            raise ValueError("The .zip file does not contain an .xml file.")
        member = max(xml_members, key=lambda i: i.file_size)
        if member.file_size > MAX_FILE_BYTES:
            raise ValueError("The XML inside the .zip is too large.")
        with zf.open(member) as fh:
            return fh.read(MAX_FILE_BYTES + 1)[:MAX_FILE_BYTES]


def load(path: str) -> UsageData:
    """Read and parse the Green Button file at ``path`` (read-only)."""
    p = resolve_path(path)
    return parse_bytes(_read_bytes(p), str(p))


def parse_bytes(data: bytes, path: str = "<memory>") -> UsageData:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"Not a readable XML file ({exc}).") from None
    except Exception as exc:  # defusedxml refuses entities / DTDs
        raise ValueError(f"XML file was refused for safety reasons ({type(exc).__name__}).") from None
    return _parse_root(root, path)


def _entries(root):
    entries = _find_all(root, "entry")
    if entries:
        return entries
    return [root]  # bare ESPI document with no Atom wrapper


def _parse_root(root, path: str) -> UsageData:
    warnings: list[str] = []
    reading_types: dict[str, ReadingTypeInfo] = {}
    rt_order: list[ReadingTypeInfo] = []
    meter_readings: list[tuple[str | None, list[str]]] = []  # (self, related)
    blocks: list[tuple[list[str], list]] = []  # (candidate hrefs, IntervalBlock elements)
    ltp = None

    for entry in _entries(root):
        links = {}
        for link in _children(entry, "link"):
            rel = link.get("rel", "")
            links.setdefault(rel, []).append(_href_key(link.get("href")))
        self_href = (links.get("self") or [None])[0]
        content = _child(entry, "content")
        scope = content if content is not None else entry

        for rt_el in ([scope] if _local(scope.tag) == "ReadingType" else _find_all(scope, "ReadingType")):
            rt = ReadingTypeInfo(
                uom=_int(rt_el, "uom"),
                power_of_ten=_int(rt_el, "powerOfTenMultiplier", 0),
                interval_length=_int(rt_el, "intervalLength"),
                flow_direction=_int(rt_el, "flowDirection"),
                commodity=_int(rt_el, "commodity"),
                href=self_href,
            )
            rt_order.append(rt)
            if self_href:
                reading_types[self_href] = rt

        if _find_all(scope, "MeterReading"):
            meter_readings.append((self_href, [h for h in links.get("related", []) if h]))

        ib_els = [scope] if _local(scope.tag) == "IntervalBlock" else _find_all(scope, "IntervalBlock")
        if ib_els:
            hrefs = [h for h in (links.get("up", []) + links.get("self", [])) if h]
            blocks.append((hrefs, ib_els))

        if ltp is None:
            found = _find_all(scope, "LocalTimeParameters")
            if found:
                ltp = found[0]

    if ltp is not None:
        local_time = LocalTime(
            tz_offset=_int(ltp, "tzOffset", 0),
            dst_offset=_int(ltp, "dstOffset", 0),
            dst_start=DstRule.decode(_text(ltp, "dstStartRule")),
            dst_end=DstRule.decode(_text(ltp, "dstEndRule")),
        )
        if local_time.dst_offset and not local_time.has_dst:
            warnings.append("DST rules in the file could not be decoded; standard time is used all year.")
    else:
        local_time = LocalTime(from_file=False)
        warnings.append("No LocalTimeParameters in the file; dates and hours are in UTC.")

    def rt_for_block(hrefs: list[str]) -> ReadingTypeInfo | None:
        for mr_self, related in meter_readings:
            rt_href = next((r for r in related if "readingtype" in r.lower()), None)
            ib_href = [r for r in related if "intervalblock" in r.lower()]
            for h in hrefs:
                if h in ib_href or (mr_self and h.startswith(mr_self + "/")):
                    if rt_href in reading_types:
                        return reading_types[rt_href]
        return None

    raw_by_series: dict[int, tuple[ReadingTypeInfo, list]] = {}
    unresolved = False
    for hrefs, ib_els in blocks:
        rt = rt_for_block(hrefs)
        if rt is None:
            if len(rt_order) >= 1:
                rt = rt_order[0]
                if len(rt_order) > 1:
                    unresolved = True
            else:
                rt = ReadingTypeInfo(uom=UOM_WH, power_of_ten=0)
                if not rt_order:
                    rt_order.append(rt)
                    warnings.append("No ReadingType in the file; values are assumed to be Wh.")
        bucket = raw_by_series.setdefault(id(rt), (rt, []))[1]
        for ib in ib_els:
            bucket.extend(_children(ib, "IntervalReading"))
    if unresolved:
        warnings.append("Some interval blocks could not be linked to a ReadingType; the first ReadingType was used.")

    series = [SeriesInfo(rt, len(els)) for rt, els in raw_by_series.values()]
    energy = [s for s in series if s.reading_type.uom == UOM_WH]
    if not energy:
        units = ", ".join(sorted({s.reading_type.unit_name for s in series})) or "none"
        raise ValueError(f"No electricity energy (Wh) interval readings found in this file (units seen: {units}).")

    def rank(s: SeriesInfo):
        flow = s.reading_type.flow_direction
        return (flow in (1, None), s.raw_count)

    primary = max(energy, key=rank)
    primary.used = True
    for s in series:
        if not s.used:
            warnings.append(
                f"The file also has a {s.reading_type.flow_name} series in {s.reading_type.unit_name} "
                f"({s.raw_count} readings); it is not included in totals."
            )

    rt = primary.reading_type
    scale = (10.0 ** rt.power_of_ten) / 1000.0  # value * 10^p Wh -> kWh
    readings_el = raw_by_series[id(rt)][1]
    return _build(path, readings_el, rt, scale, local_time, series, warnings)


def _build(path, readings_el, rt, scale, local_time, series, warnings) -> UsageData:
    by_key: dict[tuple[int, int], Reading] = {}
    duplicates = conflicting = no_value = 0
    for el in readings_el:
        tp = _child(el, "timePeriod")
        start = _int(tp, "start") if tp is not None else None
        duration = _int(tp, "duration") if tp is not None else None
        if duration is None:
            duration = rt.interval_length
        value = _int(el, "value")
        if start is None or not duration or duration <= 0 or value is None:
            no_value += 1
            continue
        reading = Reading(start, duration, value * scale)
        key = (start, duration)
        prior = by_key.get(key)
        if prior is not None:
            duplicates += 1
            if abs(prior.kwh - reading.kwh) > 1e-9:
                conflicting += 1  # keep the later reading in the file
        by_key[key] = reading

    readings = sorted(by_key.values(), key=lambda r: (r.start, r.duration))
    if not readings:
        raise ValueError("The file has no usable interval readings.")

    lengths = Counter(r.duration for r in readings)
    interval = lengths.most_common(1)[0][0]

    gaps: list[Gap] = []
    overlaps = 0
    prev_end = readings[0].end
    for r in readings[1:]:
        if r.start > prev_end:
            gaps.append(Gap(prev_end, r.start, max(1, round((r.start - prev_end) / interval))))
        elif r.start < prev_end:
            overlaps += 1
        prev_end = max(prev_end, r.end)

    if duplicates:
        msg = f"{duplicates} duplicate readings were removed"
        if conflicting:
            msg += f" ({conflicting} had different values; the one appearing later in the file was kept)"
        warnings.append(msg + ".")
    if overlaps:
        warnings.append(f"{overlaps} readings overlap the previous reading; totals may double count those periods.")
    if no_value:
        warnings.append(f"{no_value} readings were missing a start time, duration, or value and were skipped.")

    return UsageData(
        path=path,
        readings=readings,
        interval_seconds=interval,
        interval_lengths_seen=sorted(lengths),
        local_time=local_time,
        gaps=gaps,
        duplicates_removed=duplicates,
        conflicting_duplicates=conflicting,
        overlapping_readings=overlaps,
        readings_without_value=no_value,
        series=series,
        warnings=warnings,
    )


class FileCache:
    """Tiny in-memory cache keyed on (path, mtime, size). Nothing touches disk."""

    def __init__(self, size: int = 4):
        self.size = size
        self._items: dict[tuple, UsageData] = {}

    def get(self, path: str) -> UsageData:
        p = resolve_path(path)
        st = os.stat(p)
        key = (str(p), st.st_mtime_ns, st.st_size)
        if key not in self._items:
            if len(self._items) >= self.size:
                self._items.pop(next(iter(self._items)))
            self._items[key] = parse_bytes(_read_bytes(p), str(p))
        return self._items[key]
