#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Komal Thareja
#
# Author: Komal Thareja (kthare10@renci.org)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Generate QFabric sweep figures (QBER and secure key rate vs distance / attenuation).

Runs the QFabric pure-Python simulation over the sweep scenarios and writes PNGs to
paper/figures/. Reproducible and slice-free:

    python paper/make_figures.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from validation.run_qfabric import run_qfabric_bb84_simulated  # noqa: E402
from validation.scenario import ValidationScenario  # noqa: E402

OUT = ROOT / "paper" / "figures"
OUT.mkdir(parents=True, exist_ok=True)


def make_sweep_figure(sweep_name: str, xkey: str, xlabel: str) -> Path:
    """Run the QFabric simulation across a sweep and plot QBER + secure key rate."""
    scenarios = ValidationScenario.load_sweep(
        ROOT / "validation" / "scenarios" / f"{sweep_name}.yml"
    )
    xs, qber_pct, skr = [], [], []
    for s in scenarios:
        r = run_qfabric_bb84_simulated(s)
        xs.append(getattr(s, xkey))
        qber_pct.append(r.qber * 100.0)
        skr.append(r.secure_key_rate)

    fid = scenarios[0].polarization_fidelity
    expected_qber = (1.0 - fid) / 2.0 * 100.0

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
    axes[0].plot(xs, qber_pct, "o-", color="steelblue")
    axes[0].axhline(expected_qber, ls="--", color="gray",
                    label=f"expected (1−F)/2 = {expected_qber:.2f}%")
    axes[0].set(xlabel=xlabel, ylabel="QBER (%)", title=f"QBER vs {xlabel}")
    axes[0].legend()

    axes[1].plot(xs, skr, "s-", color="seagreen")
    axes[1].set(xlabel=xlabel, ylabel="Secure key rate (bits/photon)",
                title=f"Secure key rate vs {xlabel}")

    for ax in axes:
        ax.grid(alpha=0.3)
    fig.suptitle(f"QFabric simulation — {sweep_name}")
    fig.tight_layout()

    out = OUT / f"{sweep_name}.png"
    fig.savefig(out, dpi=140, bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {out.relative_to(ROOT)}")
    return out


def main() -> None:
    make_sweep_figure("sweep_distance", "distance_km", "distance (km)")
    make_sweep_figure("sweep_attenuation", "attenuation_db_per_km", "attenuation (dB/km)")
    print("done")


if __name__ == "__main__":
    main()
