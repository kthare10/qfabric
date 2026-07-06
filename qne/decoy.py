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

"""Decoy-state BB84 — photon-number-splitting-resilient key-rate analysis.

Standard BB84 (qne/bb84.py) assumes single photons; a real weak-coherent source
emits Poisson(μ) photons per pulse, and multi-photon pulses leak to a PNS attacker.
The decoy-state method sends pulses at several intensities (signal μ_s, weak decoy
μ_d, vacuum μ_v); comparing their measured gains and error rates lower-bounds the
single-photon yield Y1 and upper-bounds the single-photon error e1 (Lo–Ma–Chen,
PRL 94, 230504, 2005), giving a GLLP secure key rate that a PNS attacker can't fake.

The Lo–Ma–Chen bounds here carry the full Q_s and background (Y0) terms — the common
truncated form drops them and OVERESTIMATES Y1 (verified against the reference impl).
``decoy_state_key_rate`` is a pure analysis function (feed it measured/analytic gains
and QBERs); ``simulate_intensity`` / ``run_decoy_experiment`` provide the weak-coherent
channel model to produce those inputs. Entropy is shared with ``BB84Protocol``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from qne.bb84 import BB84Protocol

# Default intensity set and priorities (matches the reference decoy sweep).
DEFAULT_INTENSITIES = {"signal": 0.6, "decoy": 0.1, "vacuum": 0.001}


def detection_probability(n: int, eta: float, p_dc: float = 1e-6) -> float:
    """Detection probability of an n-photon pulse: 1 − (1−η)^n + p_dc (n≥1)."""
    if n == 0:
        return float(np.clip(p_dc, 0.0, 1.0))
    return float(np.clip(1.0 - (1.0 - eta) ** n + p_dc, 0.0, 1.0))


def analytic_gain(mu: float, eta: float, p_dc: float = 1e-6) -> float:
    """Expected gain Q_μ = 1 − e^{−ημ} + p_dc for a Poisson(μ) source (clipped)."""
    return float(np.clip(1.0 - np.exp(-eta * mu) + p_dc, 0.0, 1.0))


def decoy_state_key_rate(gains: dict, qbers: dict, intensities: dict,
                         f_ec: float = 1.16) -> dict:
    """GLLP + decoy secure key rate (per signal pulse) from measured statistics.

    Args:
        gains: {'signal','decoy','vacuum'} -> measured gain Q_μ (detected fraction).
        qbers: {'signal','decoy',...} -> measured QBER E_μ.
        intensities: {'signal','decoy','vacuum'} -> μ values (μ_s > μ_d > μ_v).
        f_ec: error-correction efficiency (≥ 1; 1.16 typical).

    Returns a dict with secure_key_rate, Y1_lower, e1_upper, Q1, Y0, and echoes.
    """
    mu_s = intensities["signal"]
    mu_d = intensities["decoy"]
    mu_v = intensities.get("vacuum", 0.001)

    Q_s = gains.get("signal", 0.0)
    Q_d = gains.get("decoy", 0.0)
    Q_v = gains.get("vacuum", 0.0)
    E_s = qbers.get("signal", 0.0)
    E_d = qbers.get("decoy", 0.0)

    h = BB84Protocol.binary_entropy
    denom = mu_s * mu_d - mu_d ** 2
    if denom <= 0 or Q_d <= 0:
        return {"secure_key_rate": 0.0, "Y1_lower": 0.0, "e1_upper": 0.5,
                "Q1": 0.0, "Y0": 0.0, "Q_signal": Q_s, "E_signal": E_s, "f_ec": f_ec}

    # Background (vacuum) yield: as μ_v -> 0 the vacuum gain equals Y0.
    Y0 = max(Q_v * np.exp(mu_v), 0.0)
    e0 = 0.5  # background/dark-count detections are random

    # Y1 lower bound (Lo–Ma–Chen Eq. 34), keeping the Q_s and Y0 terms.
    Y1_lower = max(0.0, (mu_s / denom) * (
        Q_d * np.exp(mu_d)
        - (mu_d ** 2 / mu_s ** 2) * Q_s * np.exp(mu_s)
        - (mu_s ** 2 - mu_d ** 2) / mu_s ** 2 * Y0
    ))
    Q1 = mu_s * np.exp(-mu_s) * Y1_lower

    # e1 upper bound (Eq. 37); background subtraction can make it ~0 at low noise.
    if Y1_lower > 0 and mu_d > 0:
        e1_upper = float(np.clip(
            (E_d * Q_d * np.exp(mu_d) - e0 * Y0) / (Y1_lower * mu_d), 0.0, 0.5))
    else:
        e1_upper = 0.5

    q = 0.5  # basis-sifting factor
    if Q_s <= 0 or E_s >= 0.5:
        skr = 0.0
    else:
        skr = max(0.0, q * (Q1 * (1.0 - h(e1_upper)) - Q_s * f_ec * h(E_s)))

    return {"secure_key_rate": float(skr), "Y1_lower": float(Y1_lower),
            "e1_upper": float(e1_upper), "Q1": float(Q1), "Y0": float(Y0),
            "Q_signal": float(Q_s), "E_signal": float(E_s), "f_ec": float(f_ec)}


def simulate_intensity(mu: float, eta: float, noise: float, num_pulses: int,
                       p_dc: float = 1e-6, rng: np.random.Generator | None = None
                       ) -> tuple[float, float]:
    """Monte-Carlo the (gain, QBER) of a weak-coherent channel at intensity μ.

    Each pulse: n ~ Poisson(μ); detected with detection_probability(n). A detected
    pulse carries a bit error with prob noise/2 (depolarizing misalignment) if a
    photon was present, or 0.5 if it was a dark count (n == 0). Returns
    (detected/total, errors/detected).
    """
    if rng is None:
        rng = np.random.default_rng()
    ns = rng.poisson(mu, size=num_pulses)
    detected = 0
    errors = 0
    for n in ns:
        p_det = detection_probability(int(n), eta, p_dc)
        if rng.random() < p_det:
            detected += 1
            p_err = 0.5 if n == 0 else noise / 2.0
            if rng.random() < p_err:
                errors += 1
    gain = detected / num_pulses if num_pulses else 0.0
    qber = errors / detected if detected else 0.0
    return gain, qber


@dataclass
class DecoyResult:
    eta: float
    noise: float
    intensities: dict
    gains: dict
    qbers: dict
    secure_key_rate: float
    Y1_lower: float
    e1_upper: float
    Q1: float
    detected_signal: int = 0
    extra: dict = field(default_factory=dict)


def run_decoy_experiment(eta: float, noise: float, *,
                         intensities: dict | None = None, num_pulses: int = 20000,
                         p_dc: float = 1e-6, f_ec: float = 1.16,
                         seed: int = 0) -> DecoyResult:
    """Simulate a 3-intensity decoy run and compute the decoy secure key rate."""
    intensities = dict(intensities or DEFAULT_INTENSITIES)
    rng = np.random.default_rng(seed)
    gains, qbers = {}, {}
    for label, mu in intensities.items():
        gains[label], qbers[label] = simulate_intensity(
            mu, eta, noise, num_pulses, p_dc=p_dc, rng=rng)
    r = decoy_state_key_rate(gains, qbers, intensities, f_ec=f_ec)
    return DecoyResult(
        eta=eta, noise=noise, intensities=intensities, gains=gains, qbers=qbers,
        secure_key_rate=r["secure_key_rate"], Y1_lower=r["Y1_lower"],
        e1_upper=r["e1_upper"], Q1=r["Q1"],
        detected_signal=int(round(gains["signal"] * num_pulses)),
        extra={"Y0": r["Y0"], "f_ec": f_ec, "p_dc": p_dc, "num_pulses": num_pulses},
    )
