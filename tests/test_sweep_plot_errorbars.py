"""The sweep plot's QBER error bars must stay honest on small samples.

`notebooks/fabric/03_all_scenarios` draws a QBER curve per backend. Those curves
are only comparable with error bars, because a measured run discloses just
`sample_fraction` of its sifted key for the estimate (disclosed bits have to be
discarded from the key) while a simulator scores the whole key -- on a real sweep
that is ~1.4k-2.5k sampled bits against 12.8k-31.9k.

The first version of those bars used the normal approximation, sqrt(p(1-p)/n),
which collapses to exactly zero whenever a sample happens to contain no errors.
The recorded sweep has a 39-bit point (distance_km=100), so a zero-error draw
there would have been painted as a certainty. Wilson stays informative at p=0 --
39 bits with no errors gives [0, 2.5%] -- and `qne/bb84.py` already implemented
it for precisely this reason.

These tests run the notebook's own plotting cell, so they fail if someone
reintroduces a Wald interval rather than only if the helper changes.
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib
import pytest

matplotlib.use("Agg")

NOTEBOOK = Path(__file__).resolve().parents[1] / "notebooks" / "fabric" / "03_all_scenarios.ipynb"


@pytest.fixture(scope="module")
def plot_group():
    """The notebook's plot_group, loaded from the notebook itself."""
    nb = json.loads(NOTEBOOK.read_text())
    cell = next(
        "".join(c["source"]) for c in nb["cells"]
        if c["cell_type"] == "code" and "def plot_group(" in "".join(c["source"])
    )
    src = "\n".join(
        line for line in cell.split("\n")
        if not line.lstrip().startswith(("%", "!"))
    )
    # the cell reads the sweep file and calls itself; we supply rows per test
    src = src.replace(
        "rows = json.loads((PROJECT_DIR / 'results' / 'all_scenarios.json').read_text())", ""
    )
    for call in ("plot_group('sweep_distance', 'distance_km', 'distance (km)')",
                 "plot_group('sweep_attenuation', 'attenuation_db_per_km', 'attenuation (dB/km)')"):
        src = src.replace(call, "")
    ns: dict = {"json": json}
    exec(compile(src, str(NOTEBOOK), "exec"), ns)
    return ns


def row(x, backends):
    return {"group": "sweep_distance", "scenario": f"distance_km={x}",
            "distance_km": x, "attenuation_db_per_km": 0.2, "backends": backends}


def backend(platform, qber, sample_bits, sifted=10000):
    return {"platform": platform, "qber": qber, "sifted_bits": sifted,
            "secure_key_rate": 0.1, "extra": {"qber_sample_bits": sample_bits}}


def bars(ns, rows):
    """Draw and return the QBER axis's (lower, upper) error extents."""
    import matplotlib.pyplot as plt
    plt.close("all")
    ns["rows"] = rows
    ns["plot_group"]("sweep_distance", "distance_km", "distance (km)")
    ax = plt.figure(plt.get_fignums()[0]).axes[0]
    container = ax.containers[0]
    segments = container.lines[2][0].get_segments()
    return [(float(seg[0][1]), float(seg[1][1])) for seg in segments]


def test_zero_error_small_sample_is_not_drawn_as_certain(plot_group):
    """39 bits with no errors must not render as a zero-height bar."""
    rows = [row(d, [backend("qfabric", 0.0, 39)]) for d in (1, 50, 100)]
    for lo, hi in bars(plot_group, rows):
        assert hi > 1.0, f"upper bound {hi}% implies near-certainty from 39 bits"
        assert lo == pytest.approx(0.0, abs=1e-9)   # cannot go below zero


def test_interval_narrows_as_the_sample_grows(plot_group):
    """More disclosed bits must mean a tighter bar."""
    widths = []
    for n in (39, 400, 2500, 30000):
        rows = [row(d, [backend("qfabric", 0.01, n)]) for d in (1, 50, 100)]
        lo, hi = bars(plot_group, rows)[0]
        widths.append(hi - lo)
    assert widths == sorted(widths, reverse=True), f"not monotonic: {widths}"


def test_measured_bar_is_wider_than_the_simulator_bar(plot_group):
    """The point of the bars: a 10% disclosed sample is far less certain than a
    whole-key score, even at the same QBER."""
    rows = [row(d, [backend("qfabric", 0.01, 2500), backend("netsquid", 0.01, 25000)])
            for d in (1, 50, 100)]
    import matplotlib.pyplot as plt
    plt.close("all")
    plot_group["rows"] = rows
    plot_group["plot_group"]("sweep_distance", "distance_km", "distance (km)")
    ax = plt.figure(plt.get_fignums()[0]).axes[0]
    widths = []
    for container in ax.containers[:2]:
        seg = container.lines[2][0].get_segments()[0]
        widths.append(float(seg[1][1]) - float(seg[0][1]))
    assert widths[0] > 2 * widths[1], f"measured bar not visibly wider: {widths}"


@pytest.mark.parametrize("qber,n", [
    (0.0, 167),     # lo = 4.3e-19, so qber - lo is negative by a rounding error
    (1.0, 6),       # hi = 0.9999999999999999, so hi - qber is negative
    (0.0, 39), (0.0, 1), (0.0, 2), (0.0, 1000),
    (1.0, 1), (1.0, 39), (1.0, 167),
])
def test_endpoint_samples_never_produce_a_negative_bar(plot_group, qber, n):
    """A QBER of exactly 0 or 1 must not crash the cell.

    wilson_interval clamps to [0, 1], but floating point can still put the bound a
    rounding error the wrong side of the estimate; matplotlib then raises
    "'yerr' must not contain negative values" and the whole sweep fails to plot.
    The n=39 case passes either way, which is why the first four tests missed this.
    """
    rows = [row(d, [backend("qfabric", qber, n)]) for d in (1, 50, 100)]
    for lo, hi in bars(plot_group, rows):      # must not raise
        assert hi >= lo


def test_a_real_39_bit_point_from_the_recorded_sweep(plot_group):
    """distance_km=100 really did sample 39 bits; it must still plot with a bar."""
    rows = [row(1, [backend("qfabric", 0.01, 2504)]),
            row(100, [backend("qfabric", 1 / 39, 39)])]
    extents = bars(plot_group, rows)
    assert len(extents) == 2
    assert (extents[1][1] - extents[1][0]) > (extents[0][1] - extents[0][0])
