import zipfile

import generate_sample as gen
import pytest
from conftest import SAMPLE, bare_block

from green_button_mcp import espi

T0 = 1761973200  # 2025-11-01 00:00 CDT


def test_sample_parses(sample_path):
    d = espi.load(sample_path)
    unique = gen.generate_readings()
    assert len(d.readings) == len(unique) == 2680
    assert d.interval_seconds == 900
    assert d.local_time.has_dst and d.local_time.tz_offset == -21600
    assert sum(r.kwh for r in d.readings) == pytest.approx(sum(wh for *_, wh in unique) / 1000)


def test_sample_has_many_blocks_one_per_day(sample_path):
    text = SAMPLE.read_text()
    assert text.count("<IntervalBlock ") == 28


def test_sample_gap_is_reported(sample_path):
    d = espi.load(sample_path)
    assert len(d.gaps) == 1
    g = d.gaps[0]
    assert g.missing_intervals == 12
    assert d.local_time.to_local(g.start).isoformat() == "2025-10-29T13:00:00-05:00"
    assert d.local_time.to_local(g.end).isoformat() == "2025-10-29T16:00:00-05:00"


def test_sample_duplicates_removed(sample_path):
    d = espi.load(sample_path)
    assert d.duplicates_removed == 4
    assert d.conflicting_duplicates == 0
    starts = [r.start for r in d.readings]
    assert len(starts) == len(set(starts))


def test_sample_is_synthetic_and_reproducible(tmp_path):
    readings = gen.generate_readings()
    assert gen.to_xml(readings) == SAMPLE.read_text(encoding="utf-8")
    assert "SYNTHETIC DATA" in SAMPLE.read_text()


@pytest.mark.parametrize(
    "power,value,expected_kwh", [(-3, 2_500_000, 2.5), (0, 2500, 2.5), (1, 250, 2.5), (3, 2, 2.0)]
)
def test_power_of_ten_multiplier(write_xml, power, value, expected_kwh):
    d = espi.load(write_xml(bare_block([(T0, 3600, value)], power=power)))
    assert d.readings[0].kwh == pytest.approx(expected_kwh)


def test_power_of_ten_on_generated_file(write_xml):
    readings = gen.generate_readings(days=2, gap=None)
    d = espi.load(write_xml(gen.to_xml(readings, power_of_ten=-3, duplicate_day=None)))
    assert sum(r.kwh for r in d.readings) == pytest.approx(sum(wh for *_, wh in readings) / 1000)


def test_non_energy_uom_rejected(write_xml):
    with pytest.raises(ValueError, match="No electricity energy"):
        espi.load(write_xml(bare_block([(T0, 900, 100)], uom=38)))


def test_conflicting_duplicates_keep_later(write_xml):
    d = espi.load(write_xml(bare_block([(T0, 900, 100), (T0, 900, 300), (T0 + 900, 900, 50)])))
    assert d.duplicates_removed == 1
    assert d.conflicting_duplicates == 1
    assert d.readings[0].kwh == pytest.approx(0.3)
    assert any("different values" in w for w in d.warnings)


def test_gap_counted_in_intervals(write_xml):
    d = espi.load(write_xml(bare_block([(T0, 900, 1), (T0 + 900 * 5, 900, 1)])))
    assert d.missing_intervals == 4


def test_out_of_order_blocks_are_sorted(write_xml):
    d = espi.load(write_xml(bare_block([(T0 + 1800, 900, 3), (T0, 900, 1), (T0 + 900, 900, 2)])))
    assert [r.start for r in d.readings] == [T0, T0 + 900, T0 + 1800]
    assert not d.gaps


def test_no_local_time_falls_back_to_utc(write_xml):
    d = espi.load(write_xml(bare_block([(T0, 900, 1)], local_time=False)))
    assert not d.local_time.from_file
    assert any("UTC" in w for w in d.warnings)


def test_bare_interval_block_without_atom(write_xml):
    xml = (
        '<IntervalBlock xmlns="http://naesb.org/espi"><IntervalReading><timePeriod>'
        f"<duration>900</duration><start>{T0}</start></timePeriod><value>250</value>"
        "</IntervalReading></IntervalBlock>"
    )
    d = espi.load(write_xml(xml))
    assert d.readings[0].kwh == pytest.approx(0.25)
    assert any("assumed to be Wh" in w for w in d.warnings)


def test_delivered_series_chosen_over_received(write_xml):
    xml = (
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        '<entry><link rel="self" href="/r/ReadingType/1"/><content><ReadingType xmlns="http://naesb.org/espi">'
        "<flowDirection>19</flowDirection><powerOfTenMultiplier>0</powerOfTenMultiplier><uom>72</uom>"
        "</ReadingType></content></entry>"
        '<entry><link rel="self" href="/r/ReadingType/2"/><content><ReadingType xmlns="http://naesb.org/espi">'
        "<flowDirection>1</flowDirection><powerOfTenMultiplier>0</powerOfTenMultiplier><uom>72</uom>"
        "</ReadingType></content></entry>"
        '<entry><link rel="self" href="/u/MeterReading/1"/><link rel="related" href="/u/MeterReading/1/IntervalBlock"/>'
        '<link rel="related" href="/r/ReadingType/1"/><content><MeterReading xmlns="http://naesb.org/espi"/></content></entry>'
        '<entry><link rel="self" href="/u/MeterReading/2"/><link rel="related" href="/u/MeterReading/2/IntervalBlock"/>'
        '<link rel="related" href="/r/ReadingType/2"/><content><MeterReading xmlns="http://naesb.org/espi"/></content></entry>'
        '<entry><link rel="up" href="/u/MeterReading/1/IntervalBlock"/><content><IntervalBlock xmlns="http://naesb.org/espi">'
        f"<IntervalReading><timePeriod><duration>900</duration><start>{T0}</start></timePeriod><value>999</value></IntervalReading>"
        "</IntervalBlock></content></entry>"
        '<entry><link rel="up" href="/u/MeterReading/2/IntervalBlock"/><content><IntervalBlock xmlns="http://naesb.org/espi">'
        f"<IntervalReading><timePeriod><duration>900</duration><start>{T0}</start></timePeriod><value>200</value></IntervalReading>"
        "</IntervalBlock></content></entry>"
        "</feed>"
    )
    d = espi.load(write_xml(xml))
    assert d.readings[0].kwh == pytest.approx(0.2)
    assert any("received (to grid)" in w for w in d.warnings)


def test_zip_download(tmp_path):
    z = tmp_path / "download.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(SAMPLE, "usage.xml")
    assert len(espi.load(str(z)).readings) == 2680


def test_entity_expansion_refused(write_xml):
    bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><x>&b;</x>'
    with pytest.raises(ValueError, match="refused"):
        espi.load(write_xml(bomb))


def test_bad_paths():
    with pytest.raises(ValueError, match="not found"):
        espi.load("/definitely/not/here.xml")
    with pytest.raises(ValueError):
        espi.load("")


def test_not_xml(write_xml):
    with pytest.raises(ValueError, match="Not a readable XML"):
        espi.load(write_xml("this is not xml"))
