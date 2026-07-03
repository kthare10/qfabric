#!/usr/bin/env python
"""Figures for the distributed-SeQUeNCe BB84 scenario sweep (see sweep.py).

Each `fig_*` builds and returns a matplotlib Figure from the sweep rows (usable
inline in a notebook); `save_all` renders them to PNGs (headless Agg). Rows carry
per-metric mean + `<metric>_std` (from sweep.py's repetitions), drawn as error bars.

    python plots.py                                  # results/sequence_scenarios.json -> results/figures/
    python plots.py --in results/x.json --out figs   # custom paths
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from qne.bb84 import BB84Protocol

PKG = Path(__file__).resolve().parent


def _legend(ax):
    if ax.get_legend_handles_labels()[1]:
        ax.legend()


def load(path) -> list[dict]:
    return json.loads(Path(path).read_text())


def _by(rows, sweep):
    """Rows for one sweep that produced a key, sorted by x."""
    sel = [r for r in rows if r.get("sweep") == sweep and r.get("key")]
    return sorted(sel, key=lambda r: r["x"])


def _xy(rows, sweep, ykey):
    """(x, y, yerr) arrays for `ykey` over a sweep; yerr from `<ykey>_std` if present."""
    sel = [r for r in _by(rows, sweep) if r.get(ykey) is not None]
    x = np.array([r["x"] for r in sel], dtype=float)
    y = np.array([r[ykey] for r in sel], dtype=float)
    stds = [r.get(ykey + "_std") for r in sel]
    yerr = np.array([s or 0.0 for s in stds]) if any(s is not None for s in stds) else None
    return x, y, yerr


def _eb(ax, x, y, yerr, fmt="o-", **kw):
    ax.errorbar(x, y, yerr=yerr, fmt=fmt, capsize=3, **kw)


def fig_qber_vs_fidelity(rows):
    import matplotlib.pyplot as plt
    x, y, e = _xy(rows, "fidelity", "qber")
    fig, ax = plt.subplots(figsize=(6, 4))
    if len(x):
        _eb(ax, x, y, e, label="emulator (sampled QBER)")
        ax.plot(x, (1 - x) / 2, "k--", alpha=0.7, label=r"analytical $(1-F)/2$")
    ax.axhline(0.11, color="red", ls=":", alpha=0.6, label="security threshold (11%)")
    ax.set(xlabel="polarization fidelity F", ylabel="QBER", title="QBER vs fidelity")
    _legend(ax); ax.grid(alpha=0.3); fig.tight_layout()
    return fig


def fig_secure_fraction(rows):
    import matplotlib.pyplot as plt
    sel = _by(rows, "fidelity")
    fig, ax = plt.subplots(figsize=(6, 4))
    qs = np.linspace(0, 0.13, 200)
    ax.plot(qs, [BB84Protocol.secure_key_fraction(q) for q in qs], "k--", alpha=0.7,
            label="Shor-Preskill $1-2H(Q)$")
    if sel:
        q = np.array([r["qber"] for r in sel])
        sf = np.array([r["secure_fraction"] for r in sel])
        xe = np.array([r.get("qber_std") or 0 for r in sel])
        ye = np.array([r.get("secure_fraction_std") or 0 for r in sel])
        ax.errorbar(q, sf, xerr=xe, yerr=ye, fmt="o", capsize=3, label="emulator")
    ax.axvline(0.11, color="red", ls=":", alpha=0.6, label="11% threshold")
    ax.set(xlabel="QBER", ylabel="secure key fraction (per sifted bit)",
           title="Secure key fraction vs QBER")
    _legend(ax); ax.grid(alpha=0.3); fig.tight_layout()
    return fig


def fig_distance(rows):
    import matplotlib.pyplot as plt
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4))
    xs, ys, es = _xy(rows, "distance", "sifted_bits")
    if len(xs):
        _eb(ax1, xs, ys, es, label="sifted bits")
        xf, yf, ef = _xy(rows, "distance", "final_key_bits")
        _eb(ax1, xf, yf, ef, fmt="s-", label="final secure bits")
        ax1.set(xlabel="distance (km)", ylabel="bits", title="Key yield vs distance")
        _legend(ax1); ax1.grid(alpha=0.3)
        xl, yl, _ = _xy(rows, "distance", "loss_probability")
        ax2.plot(xl, yl, "^-", color="darkorange")
        ax2.set(xlabel="distance (km)", ylabel="channel loss probability",
                title=r"Fiber loss $1-10^{-\alpha L/10}$ vs distance")
        ax2.grid(alpha=0.3)
    fig.tight_layout()
    return fig


def fig_attenuation(rows):
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(6, 4))
    xs, ys, es = _xy(rows, "attenuation", "sifted_bits")
    if len(xs):
        _eb(ax, xs, ys, es, label="sifted bits")
        xf, yf, ef = _xy(rows, "attenuation", "final_key_bits")
        _eb(ax, xf, yf, ef, fmt="s-", label="final secure bits")
    ax.set(xlabel="attenuation (dB/km)", ylabel="bits", title="Key yield vs attenuation")
    _legend(ax); ax.grid(alpha=0.3); fig.tight_layout()
    return fig


def fig_efficiency(rows):
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(6, 4))
    xs, ys, es = _xy(rows, "efficiency", "sifted_bits")
    if len(xs):
        _eb(ax, xs, ys, es, label="sifted bits")
        xf, yf, ef = _xy(rows, "efficiency", "final_key_bits")
        _eb(ax, xf, yf, ef, fmt="s-", label="final secure bits")
    ax.set(xlabel="detector efficiency", ylabel="bits",
           title="Key yield vs detector efficiency")
    _legend(ax); ax.grid(alpha=0.3); fig.tight_layout()
    return fig


def fig_dark_count(rows):
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(6, 4))
    x, y, e = _xy(rows, "dark_count", "qber")
    if len(x):
        # rate 0 can't plot on a log axis; show it as the leftmost decade
        xp = np.where(x <= 0, min([v for v in x if v > 0], default=1.0) / 10, x)
        ax.errorbar(xp, y, yerr=e, fmt="o-", capsize=3, label="emulator QBER")
        ax.set_xscale("log")
    ax.axhline(0.11, color="red", ls=":", alpha=0.6, label="11% threshold")
    ax.set(xlabel="dark count rate (Hz)", ylabel="QBER",
           title="QBER vs dark-count rate")
    _legend(ax); ax.grid(alpha=0.3, which="both"); fig.tight_layout()
    return fig


def fig_sample_fraction(rows):
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(6, 4))
    x, y, e = _xy(rows, "sample_fraction", "qber")
    if len(x):
        _eb(ax, x, y, e, label="QBER (mean ± std over reps)")
    ax.set(xlabel="disclosure sample fraction", ylabel="QBER",
           title="QBER estimate vs sample fraction (error bar shrinks)")
    _legend(ax); ax.grid(alpha=0.3); fig.tight_layout()
    return fig


def fig_key_length(rows):
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(6, 4))
    x, y, e = _xy(rows, "key_length", "final_key_bits")
    if len(x):
        _eb(ax, x, y, e, label="final secure bits")
    # mark points where a key failed to form (n_ok < n_reps)
    fails = [r for r in rows if r.get("sweep") == "key_length" and r.get("n_ok", 1) < r.get("n_reps", 1)]
    for r in fails:
        ax.axvline(r["x"], color="red", ls=":", alpha=0.4)
    ax.set(xlabel="target key length (bits)", ylabel="final secure bits",
           title="Key formation vs target length (dotted = some reps failed)")
    _legend(ax); ax.grid(alpha=0.3); fig.tight_layout()
    return fig


def fig_throughput(rows):
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(6, 4))
    sel = [r for r in rows if r.get("sweep") == "throughput" and r.get("key")]
    for mode, fmt in (("bulk", "o-"), ("per_event", "s-")):
        pts = sorted((r for r in sel if r["photon_mode"] == mode), key=lambda r: r["x"])
        if pts:
            x = np.array([r["x"] for r in pts], dtype=float)
            y = np.array([r["photons_per_s"] for r in pts], dtype=float)
            e = np.array([r.get("photons_per_s_std") or 0 for r in pts])
            ax.errorbar(x, y, yerr=e, fmt=fmt, capsize=3, label=mode)
    ax.set(xlabel="photons emitted", ylabel="photons / second",
           title="Throughput: bulk vs per_event")
    _legend(ax); ax.grid(alpha=0.3); fig.tight_layout()
    return fig


def fig_network_effects(rows):
    """Classical-channel netem sweep (FABRIC): QBER stays flat (TCP reliable) while
    time-to-key / key-rate degrade. `rows` carry a `condition` label."""
    import matplotlib.pyplot as plt
    sel = [r for r in rows if r.get("condition") and r.get("key")]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4))
    if sel:
        names = [r["condition"] for r in sel]
        x = np.arange(len(names))
        ax1.bar(x, [r.get("qber", 0) for r in sel], color="steelblue")
        ax1.axhline(0.11, color="red", ls=":", alpha=0.6, label="11% threshold")
        ax1.set_xticks(x); ax1.set_xticklabels(names, rotation=30, ha="right")
        ax1.set(ylabel="QBER", title="QBER vs classical-channel condition")
        _legend(ax1); ax1.grid(alpha=0.3, axis="y")
        ax2.bar(x, [r.get("key_bits_per_s", 0) for r in sel], color="seagreen")
        ax2.set_xticks(x); ax2.set_xticklabels(names, rotation=30, ha="right")
        ax2.set(ylabel="secure key bits / second",
                title="Key rate vs classical-channel condition")
        ax2.grid(alpha=0.3, axis="y")
    fig.tight_layout()
    return fig


_FIGS = {
    "qber_vs_fidelity": fig_qber_vs_fidelity,
    "secure_fraction": fig_secure_fraction,
    "distance": fig_distance,
    "attenuation": fig_attenuation,
    "efficiency": fig_efficiency,
    "dark_count": fig_dark_count,
    "sample_fraction": fig_sample_fraction,
    "key_length": fig_key_length,
    "throughput": fig_throughput,
}


def save_all(in_path, out_dir) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    rows = load(in_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    saved = []
    for name, fn in _FIGS.items():
        fig = fn(rows)
        p = out / f"seq_{name}.png"
        fig.savefig(p, dpi=130)
        saved.append(str(p))
    return saved


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Plot the SeQUeNCe-emulator scenario sweep.")
    ap.add_argument("--in", dest="inp", default=str(PKG / "results" / "sequence_scenarios.json"))
    ap.add_argument("--out", default=str(PKG / "results" / "figures"))
    args = ap.parse_args(argv)
    for p in save_all(args.inp, args.out):
        print("wrote", p)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
