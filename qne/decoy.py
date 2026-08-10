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
The decoy-state method (Lo, Ma & Chen, PRL 94, 230504, 2005) sends pulses at several
intensities (signal μ_s, weak decoy μ_d, vacuum μ_v); comparing their measured gains
and error rates lower-bounds the single-photon yield Y1 and upper-bounds the
single-photon error e1, giving a GLLP secure key rate a PNS attacker can't fake
(Gottesman, Lo, Lütkenhaus & Preskill, Quantum Inf. Comput. 4, 325, 2004).

The bounds implemented below take the algebraic form of Eqs. (34) and (37) of
Ma, Qi, Zhao & Lo, "Practical decoy state for quantum key distribution", Phys. Rev. A
72, 012326 (2005), arXiv:quant-ph/0503005 — checked term for term against that paper.
They are kept in full: the commonly quoted truncated form drops the Q_s and background
(Y0) terms and OVERESTIMATES Y1, i.e. it errs in the unsafe direction.

NEAR-VACUUM HANDLING. Ma et al.'s Vacuum+Weak protocol assumes a TRUE vacuum
(mu_v = 0), where the measured vacuum gain IS Y0. DEFAULT_INTENSITIES ships a
near-vacuum mu_v = 1e-3, whose gain also counts single-photon detections:
Q_v e^{mu_v} = Y0 + mu_v*Y1 + O(mu_v^2), which at eta = 0.5, p_dc = 1e-6 is ~500x the
true dark-count floor. Y0 is SUBTRACTED in both bounds, so a single estimator cannot
be conservative for both — Eq. 34 (lower-bounding Y1) needs Y0 from ABOVE and Eq. 37
(upper-bounding e1) needs it from BELOW. We therefore straddle it with Y0_upper =
Q_v e^{mu_v} and Y0_lower = Q_v e^{mu_v} - (e^{mu_v} - 1), the second subtracting an
upper bound on the ENTIRE single-and-multi-photon tail (every Y_n <= 1). At mu_v = 0
the two collapse onto the measured Q_v and this is exactly the paper.

Two traps, both of which this code hit and now regression-tests:
  * Using the UPPER estimate in Eq. 37 makes e1_upper smaller than the true e1 — not
    a bound at all — and inflated the reported rate by ~3%.
  * Subtracting only the n=1 term (mu_v) for the lower estimate drops mu_v^2/2 + ...
    and is likewise not conservative; at mu_v = 0.1 on a transparent channel it
    reports Y0 ~ 5e-3 against a true 1e-6.
The cost of a near-vacuum is a looser e1, never an unsafe one.
``decoy_state_key_rate`` is a pure analysis function (feed it measured/analytic gains
and QBERs); ``simulate_intensity`` / ``run_decoy_experiment`` provide the weak-coherent
channel model to produce those inputs. Entropy is shared with ``BB84Protocol``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from qne.bb84 import BB84Protocol

# Default intensity set (matches the reference decoy sweep). NOTE: vacuum is a
# NEAR-vacuum; see the module docstring caveat on the Y0 estimator and e1 bound.
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

    # Background (vacuum) yield. Y0 is SUBTRACTED in both bounds below, so each
    # needs the opposite side of the interval to stay conservative: Eq. 34 (a lower
    # bound on Y1) needs Y0 from above, Eq. 37 (an upper bound on e1) needs it from
    # below. With a true vacuum (mu_v = 0) both collapse to the measured Q_v and
    # this is exactly Ma et al.'s Vacuum+Weak protocol; for the near-vacuum default
    # they straddle it, because Q_v then also counts single-photon detections.
    Y0_upper = max(Q_v * np.exp(mu_v), 0.0)
    # Q_v e^{mu_v} = Y0 + sum_{n>=1} (mu_v^n / n!) Y_n, so a LOWER bound on Y0 must
    # subtract an UPPER bound on that whole tail. Every Y_n <= 1, hence the tail is
    # at most sum_{n>=1} mu_v^n/n! = e^{mu_v} - 1. (Subtracting only the n=1 term,
    # mu_v, drops the mu_v^2/2 + ... remainder and is NOT conservative -- at mu_v =
    # 0.1 on a transparent channel it overstates Y0 by ~5e-3 against a true 1e-6.)
    # expm1, not exp(mu_v) - 1.0: the latter cancels catastrophically for tiny mu_v
    # and rounds to exactly 0.0 below the double eps (~2.2e-16), which silently drops
    # the whole tail correction and pushes Y0_lower back above the true Y0.
    Y0_lower = max(Q_v * np.exp(mu_v) - np.expm1(mu_v), 0.0)
    e0 = 0.5  # background/dark-count detections are random

    # Y1 lower bound (Ma–Qi–Zhao–Lo Eq. 34), keeping the Q_s and Y0 terms.
    Y1_lower = max(0.0, (mu_s / denom) * (
        Q_d * np.exp(mu_d)
        - (mu_d ** 2 / mu_s ** 2) * Q_s * np.exp(mu_s)
        - (mu_s ** 2 - mu_d ** 2) / mu_s ** 2 * Y0_upper
    ))
    Q1 = mu_s * np.exp(-mu_s) * Y1_lower

    # e1 upper bound (Ma–Qi–Zhao–Lo Eq. 37).
    if Y1_lower > 0 and mu_d > 0:
        e1_upper = float(np.clip(
            (E_d * Q_d * np.exp(mu_d) - e0 * Y0_lower) / (Y1_lower * mu_d), 0.0, 0.5))
    else:
        e1_upper = 0.5

    q = 0.5  # basis-sifting factor
    if Q_s <= 0 or E_s >= 0.5:
        skr = 0.0
    else:
        skr = max(0.0, q * (Q1 * (1.0 - h(e1_upper)) - Q_s * f_ec * h(E_s)))

    return {"secure_key_rate": float(skr), "Y1_lower": float(Y1_lower),
            "e1_upper": float(e1_upper), "Q1": float(Q1), "Y0": float(Y0_upper),
            "Y0_lower": float(Y0_lower),
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
