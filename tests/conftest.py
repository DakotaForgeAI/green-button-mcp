import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

SAMPLE = ROOT / "samples" / "synthetic_green_button.xml"


@pytest.fixture
def sample_path() -> str:
    return str(SAMPLE)


@pytest.fixture
def write_xml(tmp_path):
    def _write(text: str, name: str = "data.xml") -> str:
        p = tmp_path / name
        p.write_text(text, encoding="utf-8")
        return str(p)

    return _write


def bare_block(readings, uom=72, power=0, flow=1, local_time=True) -> str:
    """Minimal Atom feed: optional LocalTimeParameters + one ReadingType + one block."""
    ltp = (
        '<entry><content><LocalTimeParameters xmlns="http://naesb.org/espi">'
        "<dstEndRule>B40E2000</dstEndRule><dstOffset>3600</dstOffset>"
        "<dstStartRule>360E2000</dstStartRule><tzOffset>-21600</tzOffset>"
        "</LocalTimeParameters></content></entry>"
        if local_time
        else ""
    )
    irs = "".join(
        f"<IntervalReading><timePeriod><duration>{d}</duration><start>{s}</start></timePeriod>"
        f"<value>{v}</value></IntervalReading>"
        for s, d, v in readings
    )
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        f"{ltp}"
        '<entry><content><ReadingType xmlns="http://naesb.org/espi">'
        f"<flowDirection>{flow}</flowDirection><powerOfTenMultiplier>{power}</powerOfTenMultiplier>"
        f"<uom>{uom}</uom></ReadingType></content></entry>"
        f'<entry><content><IntervalBlock xmlns="http://naesb.org/espi">{irs}</IntervalBlock></content></entry>'
        "</feed>"
    )
