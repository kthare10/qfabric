#!/usr/bin/env python
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Komal Thareja
#
# Author: Komal Thareja (kthare10@renci.org)

"""Decoy-state BB84 sweep — secure key rate vs channel efficiency and noise.

Monte-Carlo the weak-coherent (Poisson-source) channel at three intensities and
apply the Lo-Ma-Chen decoy bounds (qne/decoy.py), one axis at a time with
repetitions (mean +/- std). Slice-free and reproducible.

    python scripts/decoy_sweep.py                 # full matrix -> results/decoy_scenarios.json
    python scripts/decoy_sweep.py --quick         # fast smoke matrix
    python scripts/decoy_sweep.py --reps 20 --plot # more reps + save a figure

Reference sweep values follow the sibling quantum-weave 2b scenario:
  eta   {0.01, 0.05, 0.1, 0.2, 0.5}  (channel/detector efficiency)
  noise {0, 0.01, 0.03, 0.05, 0.07, 0.10, 0.15}  (depolarizing misalignment)
  mu = 0.6 / 0.1 / 0.001, p_dc = 1e-6, f_ec = 1.16.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

from qne.decoy import DEFAULT_INTENSITIES, run_decoy_experiment

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"

ETA_AXIS = [0.01, 0.05, 0.1, 0.2, 0.5]
NOISE_AXIS = [0.0, 0.01, 0.03, 0.05, 0.07, 0.10, 0.15]
FIXED_NOISE = 0.02          # when sweeping eta
FIXED_ETA = 0.2             # when sweeping noise


def _aggregate(eta, noise, reps, num_pulses, base_seed):
    runs = [run_decoy_experiment(eta, noise, num_pulses=num_pulses, seed=base_seed + r)
            for r in range(reps)]
    def mean_std(attr):
        vals = [getattr(x, attr) for x in runs]
        return statistics.mean(vals), (statistics.stdev(vals) if len(vals) > 1 else 0.0)
    skr_m, skr_s = mean_std("secure_key_rate")
    y1_m, _ = mean_std("Y1_lower")
    e1_m, _ = mean_std("e1_upper")
    return {
        "eta": eta, "noise": noise, "reps": reps,
        "secure_key_rate": skr_m, "secure_key_rate_std": skr_s,
        "Y1_lower": y1_m, "e1_upper": e1_m,
        "gain_signal": statistics.mean(x.gains["signal"] for x in runs),
        "qber_signal": statistics.mean(x.qbers["signal"] for x in runs),
    }


def run_sweep(reps=10, num_pulses=20000, quick=False):
    if quick:
        eta_axis, noise_axis, reps, num_pulses = [0.1, 0.5], [0.0, 0.05], 3, 4000
    else:
        eta_axis, noise_axis = ETA_AXIS, NOISE_AXIS
    rows = []
    for eta in eta_axis:
        row = _aggregate(eta, FIXED_NOISE, reps, num_pulses, base_seed=100)
        row["sweep"], row["x"] = "efficiency", eta
        rows.append(row)
        print(f"  eta={eta:<5} noise={FIXED_NOISE}: SKR={row['secure_key_rate']:.4e} "
              f"Y1={row['Y1_lower']:.4f} e1={row['e1_upper']:.4f}")
    for noise in noise_axis:
        row = _aggregate(FIXED_ETA, noise, reps, num_pulses, base_seed=200)
        row["sweep"], row["x"] = "noise", noise
        rows.append(row)
        print(f"  eta={FIXED_ETA} noise={noise:<5}: SKR={row['secure_key_rate']:.4e} "
              f"qber={row['qber_signal']:.4f}")
    return {"intensities": DEFAULT_INTENSITIES, "rows": rows}


def make_figure(data, out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    eff = [r for r in data["rows"] if r["sweep"] == "efficiency"]
    noi = [r for r in data["rows"] if r["sweep"] == "noise"]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 4))
    ax1.errorbar([r["x"] for r in eff], [r["secure_key_rate"] for r in eff],
                 yerr=[r["secure_key_rate_std"] for r in eff], marker="o")
    ax1.set(xlabel="channel efficiency η", ylabel="secure key rate / pulse",
            title="Decoy-state SKR vs efficiency", xscale="log", yscale="log")
    ax1.grid(True, alpha=0.3)
    ax2.plot([r["x"] for r in noi], [r["secure_key_rate"] for r in noi], marker="s")
    ax2.set(xlabel="depolarizing noise", ylabel="secure key rate / pulse",
            title=f"Decoy-state SKR vs noise (η={FIXED_ETA})")
    ax2.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"figure -> {out}")


def main():
    ap = argparse.ArgumentParser(description="Decoy-state BB84 sweep")
    ap.add_argument("--reps", type=int, default=10)
    ap.add_argument("--num-pulses", type=int, default=20000)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--plot", action="store_true", help="also save a figure")
    ap.add_argument("--out", default=str(RESULTS / "decoy_scenarios.json"))
    args = ap.parse_args()

    print("=== Decoy-state BB84 sweep ===")
    data = run_sweep(reps=args.reps, num_pulses=args.num_pulses, quick=args.quick)
    RESULTS.mkdir(exist_ok=True)
    Path(args.out).write_text(json.dumps(data, indent=2))
    print(f"results -> {args.out}")
    if args.plot:
        make_figure(data, ROOT / "paper" / "figures" / "decoy_sweep.png")


if __name__ == "__main__":
    main()
