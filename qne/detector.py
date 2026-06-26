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
- Basis measurement (random basis choice, deterministic outcome if matched)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from qne.photon import PhotonPacket


@dataclass
class DetectionEvent:
    """Result of a detector measurement."""
    sequence_num: int
    detected: bool
    basis: int          # Measurement basis chosen by Bob
    bit_value: int      # Measured bit value (meaningful only if detected)
    is_dark_count: bool = False
    basis_match: bool = False


class Detector:
    """Single-photon detector model.

    Attributes:
        efficiency: Probability of detecting a real photon (0.0 to 1.0).
        dark_count_rate: Dark count rate in Hz.
        detection_window: Time window per photon slot in seconds.
        rng: NumPy random generator.
    """

    def __init__(
        self,
        efficiency: float = 0.8,
        dark_count_rate: float = 10.0,
        detection_window: float = 1e-9,  # 1 ns default window
        polarization_error: float = 0.0,
        seed: Optional[int] = None,
    ):
        self.efficiency = efficiency
        self.dark_count_rate = dark_count_rate
        self.detection_window = detection_window
        # Depolarizing probability from channel/optics polarization imperfection.
        # On a matched basis the qubit is randomized with this probability, so the
        # intrinsic QBER it contributes is polarization_error / 2. Derive it from a
        # polarization fidelity F as polarization_error = 1 - F.
        self.polarization_error = polarization_error
        self.rng = np.random.default_rng(seed)

        # Dark count probability per detection window
        self.dark_count_prob = dark_count_rate * detection_window

    def detect(self, photon: PhotonPacket) -> DetectionEvent:
        """Simulate detection of a single photon.

        1. Choose a random measurement basis.
        2. If basis matches: bit value = photon's state, unless polarization
           imperfection depolarizes it (probability polarization_error), in
           which case the outcome is random — this is the intrinsic QBER source.
        3. If basis mismatches: random bit value (50/50).
        4. Apply detector efficiency (probability of actually detecting).
        5. Dark count check (can trigger even if photon missed).

        Args:
            photon: The incoming photon packet.

        Returns:
            DetectionEvent describing the measurement outcome.
        """
        # Bob picks a random basis
        meas_basis = self.rng.integers(0, 2)
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

        # Apply detector efficiency
        detected = bool(self.rng.random() < self.efficiency)

        # Check for dark count (even if not detected via efficiency)
        is_dark = False
        if not detected and self.dark_count_prob > 0:
            is_dark = bool(self.rng.random() < self.dark_count_prob)
            if is_dark:
                detected = True
                bit_value = int(self.rng.integers(0, 2))

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
