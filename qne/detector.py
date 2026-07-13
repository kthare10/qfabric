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

"""Detector model for BB84 quantum key distribution.

Models single-photon detector characteristics:
- Detection efficiency (probability of registering a real photon)
- Dark counts (false detections from thermal noise)
- Basis measurement (random basis choice, deterministic outcome if matched);
  the choice can be *biased* (efficient BB84: P(Z) = basis_bias > 1/2)
- Dead time (the detector is blind for dead_time ns after each click)
- Timing jitter (a click whose gaussian timing error falls outside the
  detection window is discarded — effective efficiency × erf(w/(2√2·σ)))
- Multi-photon pulses (decoy-state source: an n-photon pulse fires with
  probability 1 − (1−η)^n)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np

from qne.photon import PhotonPacket


@dataclass
class DetectionEvent:
    """Result of a detector measurement."""
    sequence_num: int
    detected: bool
    basis: int          # Measurement basis chosen by Bob (-1 if dead)
    bit_value: int      # Measured bit value (meaningful only if detected)
    is_dark_count: bool = False
    basis_match: bool = False
    is_dead_time: bool = False   # arrival fell inside the dead window


class Detector:
    """Single-photon detector model.

    Attributes:
        efficiency: Probability of detecting a real photon (0.0 to 1.0).
        dark_count_rate: Dark count rate in Hz.
        detection_window: Time window per photon slot in seconds.
        basis_bias: P(measure in Z). 0.5 = standard BB84; >0.5 = efficient BB84.
        dead_time: blind interval after each click, in ns (0 = not modeled).
        timing_jitter: gaussian σ of the click time, in ns (0 = not modeled).
        pulse_period_ns: emulated slot spacing; arrival time of photon k is
            k·pulse_period_ns. Needed (or an explicit arrival time per photon)
            for dead-time gating.
        rng: NumPy random generator.
    """

    def __init__(
        self,
        efficiency: float = 0.8,
        dark_count_rate: float = 10.0,
        detection_window: float = 1e-9,  # 1 ns default window
        polarization_error: float = 0.0,
        seed: Optional[int] = None,
        basis_bias: float = 0.5,
        dead_time: float = 0.0,          # ns (matches DetectorConfig)
        timing_jitter: float = 0.0,      # ns (matches DetectorConfig)
        pulse_period_ns: float = 0.0,
    ):
        self.efficiency = efficiency
        self.dark_count_rate = dark_count_rate
        self.detection_window = detection_window
        # Depolarizing probability from channel/optics polarization imperfection.
        # On a matched basis the qubit is randomized with this probability, so the
        # intrinsic QBER it contributes is polarization_error / 2. Derive it from a
        # polarization fidelity F as polarization_error = 1 - F.
        self.polarization_error = polarization_error
        self.basis_bias = basis_bias
        self.dead_time = dead_time
        self.timing_jitter = timing_jitter
        self.pulse_period_ns = pulse_period_ns
        self.rng = np.random.default_rng(seed)

        # Dark count probability per detection window
        self.dark_count_prob = dark_count_rate * detection_window
        self._blocked_until_ns = -math.inf   # dead-time horizon
        self.dead_time_drops = 0

    def jitter_pass_probability(self) -> float:
        """P(a click's gaussian timing error lands inside the gate): erf(w/(2√2σ))."""
        if self.timing_jitter <= 0:
            return 1.0
        w_ns = self.detection_window * 1e9
        return math.erf(w_ns / (2.0 * math.sqrt(2.0) * self.timing_jitter))

    def _arrival_ns(self, photon: PhotonPacket,
                    arrival_time_ns: float | None) -> float | None:
        if arrival_time_ns is not None:
            return arrival_time_ns
        if self.pulse_period_ns > 0:
            return photon.sequence_num * self.pulse_period_ns
        return None

    def detect(self, photon: PhotonPacket,
               arrival_time_ns: float | None = None) -> DetectionEvent:
        """Simulate detection of a single photon.

        1. Dead-time gate: inside the blind window nothing registers (needs an
           arrival time — explicit, or derived from pulse_period_ns).
        2. Choose a measurement basis (P(Z) = basis_bias).
        3. If basis matches: bit value = photon's state, unless polarization
           imperfection depolarizes it (probability polarization_error), in
           which case the outcome is random — this is the intrinsic QBER source.
        4. If basis mismatches: random bit value (50/50).
        5. Apply detector efficiency, then the timing-jitter gate (a click whose
           gaussian timing error exceeds half the window is discarded).
        6. Dark count check (can trigger even if the photon missed).
        7. A click (real or dark) re-arms the dead-time window.
        """
        return self._detect(photon, self.efficiency,
                            self._arrival_ns(photon, arrival_time_ns))

    def detect_pulse(self, photon: PhotonPacket, n_photons: int,
                     arrival_time_ns: float | None = None) -> DetectionEvent:
        """Detect a (possibly multi-photon) pulse carrying ``n_photons``.

        Decoy-state sources emit Poisson(μ) photons per pulse; with per-photon
        efficiency η the pulse fires with probability 1 − (1−η)^n. An empty pulse
        (n = 0, vacuum or fully lost) can only register as a dark count.
        """
        p = 0.0 if n_photons <= 0 else 1.0 - (1.0 - self.efficiency) ** n_photons
        return self._detect(photon, p, self._arrival_ns(photon, arrival_time_ns))

    def _detect(self, photon: PhotonPacket, p_detect: float,
                t_ns: float | None) -> DetectionEvent:
        # Dead-time gate: a blind detector registers nothing, not even darks.
        if self.dead_time > 0 and t_ns is not None and t_ns < self._blocked_until_ns:
            self.dead_time_drops += 1
            return DetectionEvent(
                sequence_num=photon.sequence_num, detected=False,
                basis=-1, bit_value=0, is_dead_time=True,
            )

        # Bob picks a basis: Z with prob basis_bias (0.5 = unbiased BB84)
        meas_basis = 0 if self.rng.random() < self.basis_bias else 1
        basis_match = (meas_basis == photon.basis)

        # Determine bit value
        if basis_match:
            # Polarization imperfection: with prob polarization_error the qubit
            # is depolarized, randomizing the outcome (contributes QBER ~ p/2).
            if self.polarization_error > 0 and self.rng.random() < self.polarization_error:
                bit_value = int(self.rng.integers(0, 2))
            else:
                bit_value = photon.state
        else:
            bit_value = int(self.rng.integers(0, 2))

        # Apply detector efficiency (per-pulse probability for multi-photon)
        detected = bool(self.rng.random() < p_detect)

        # Timing jitter: the click's timing error must land inside the gate
        if detected and self.timing_jitter > 0:
            w_ns = self.detection_window * 1e9
            detected = abs(self.rng.normal(0.0, self.timing_jitter)) <= w_ns / 2.0

        # Check for dark count (even if photon missed / was gated out)
        is_dark = False
        if not detected and self.dark_count_prob > 0:
            is_dark = bool(self.rng.random() < self.dark_count_prob)
            if is_dark:
                detected = True
                bit_value = int(self.rng.integers(0, 2))

        # A click re-arms the dead-time window
        if detected and self.dead_time > 0 and t_ns is not None:
            self._blocked_until_ns = t_ns + self.dead_time

        return DetectionEvent(
            sequence_num=photon.sequence_num,
            detected=detected,
            basis=meas_basis,
            bit_value=bit_value,
            is_dark_count=is_dark,
            basis_match=basis_match,
        )

    def detect_batch(self, photons: list[PhotonPacket]) -> list[DetectionEvent]:
        """Detect a batch of photons."""
        return [self.detect(p) for p in photons]

    def generate_dark_counts(self, num_slots: int) -> list[int]:
        """Generate dark count events for empty time slots.

        Returns sequence numbers of slots where dark counts occurred.
        Used when photons are lost in the channel but the detector
        still has a chance of registering a dark count.
        """
        dark_slots = []
        for slot in range(num_slots):
            if self.rng.random() < self.dark_count_prob:
                dark_slots.append(slot)
        return dark_slots
