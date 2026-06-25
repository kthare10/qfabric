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

"""Independent analytic BB84 baseline — used by the TEST SUITE only.

This is a *separately written* implementation of the same physical model used by
the QFabric emulator (channel loss, polarization-misalignment QBER, detector
efficiency, dark counts) — it does NOT import qne.detector / qne.bb84. The test
suite uses it to sanity-check the QFabric emulator: agreement between this
independent codepath and the QFabric path catches regressions in the emulator.

It is NOT a simulator backend and is NOT used by the SeQUeNCe or NetSquid
adapters — those (validation/run_sequence.py, validation/run_netsquid.py) drive
the real SeQUeNCe and NetSquid engines so cross-validation reflects each
simulator's own physics, not a re-implementation.

Physics (matches SPEC.md):
    loss      P(loss) = 1 - 10^(-alpha * L / 10)
    QBER_mis  e = (1 - polarization_fidelity) / 2     (depolarizing misalignment)
    detector  efficiency eta, dark-count prob = rate * detection_window
    sifting   bases agree with probability 1/2
    SKR       max(0, 1 - 2 H(QBER)) per sifted bit (Shor-Preskill, QBER < 11%)
"""

from __future__ import annotations

import numpy as np

from validation.scenario import ValidationResult, ValidationScenario


def _binary_entropy(p: float) -> float:
    if p <= 0.0 or p >= 1.0:
        return 0.0
    return float(-p * np.log2(p) - (1 - p) * np.log2(1 - p))


def reference_bb84(
    scenario: ValidationScenario,
    platform: str,
    detection_window: float = 1e-9,
    extra: dict | None = None,
) -> ValidationResult:
    """Run the independent reference BB84 Monte Carlo for a scenario.

    Args:
        scenario: Parameters (distance, attenuation, efficiency, fidelity, ...).
        platform: Label for the result ("sequence", "netsquid", ...).
        detection_window: Dark-count integration window in seconds.
        extra: Extra metadata to attach to the result.

    Returns:
        ValidationResult with photons sent/received, sifted bits, QBER, and rates.
    """
    rng = np.random.default_rng(scenario.seed)
    n = scenario.num_photons

    loss = scenario.expected_loss_probability
    eta = scenario.detector_efficiency
    e_mis = (1.0 - scenario.polarization_fidelity) / 2.0
    p_dc = scenario.dark_count_rate_hz * detection_window

    # Channel + detector: a photon is registered if it survives the fiber and
    # the detector fires (efficiency), or — on an otherwise-empty slot — a dark
    # count fires. Real and dark detections are mutually exclusive here.
    survive = rng.random(n) < (1.0 - loss)
    detected_real = survive & (rng.random(n) < eta)
    dark = (~detected_real) & (rng.random(n) < p_dc)
    detected = detected_real | dark

    # Sifting: Alice and Bob pick the same basis with probability 1/2.
    basis_match = rng.random(n) < 0.5
    sifted_mask = detected & basis_match
    sifted = int(sifted_mask.sum())

    # Errors on sifted bits: real detections flip with the misalignment rate;
    # dark counts are uniformly random (error probability 1/2).
    err_prob = np.where(detected_real, e_mis, 0.5)
    errors = int((sifted_mask & (rng.random(n) < err_prob)).sum())
    qber = errors / sifted if sifted > 0 else 0.0

    # Secure key rate (asymptotic Shor-Preskill), consistent with qne.bb84.
    if 0.0 <= qber < 0.11:
        secure_fraction = max(0.0, 1.0 - 2.0 * _binary_entropy(qber))
    else:
        secure_fraction = 0.0

    raw_key_rate = sifted / n if n > 0 else 0.0
    secure_key_rate = (sifted * secure_fraction) / n if n > 0 else 0.0

    meta = {
        "loss_probability": float(loss),
        "intrinsic_qber": float(e_mis),
        "model": "independent reference Monte Carlo (qfabric-parameterised)",
    }
    if extra:
        meta.update(extra)

    return ValidationResult(
        platform=platform,
        scenario_name=scenario.name,
        photons_sent=n,
        photons_received=int(detected.sum()),
        sifted_bits=sifted,
        qber=qber,
        raw_key_rate=raw_key_rate,
        secure_key_rate=secure_key_rate,
        extra=meta,
    )
