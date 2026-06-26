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

"""Tests for the detector model."""

import numpy as np

from qne.detector import Detector, DetectionEvent
from qne.photon import PhotonPacket


class TestDetectorEfficiency:
    """Test detector efficiency model."""

    def test_perfect_efficiency(self):
        """Efficiency=1.0 → all photons detected."""
        det = Detector(efficiency=1.0, dark_count_rate=0, seed=42)
        photons = [
            PhotonPacket(basis=0, state=0, sequence_num=i) for i in range(1000)
        ]
        events = det.detect_batch(photons)
        detected = sum(1 for e in events if e.detected)
        assert detected == 1000

    def test_zero_efficiency_no_dark(self):
        """Efficiency=0.0, no dark counts → no detections."""
        det = Detector(efficiency=0.0, dark_count_rate=0, seed=42)
        photons = [
            PhotonPacket(basis=0, state=0, sequence_num=i) for i in range(1000)
        ]
        events = det.detect_batch(photons)
        detected = sum(1 for e in events if e.detected)
        assert detected == 0

    def test_partial_efficiency(self):
        """Efficiency=0.5 → ~50% detection rate (within 3σ)."""
        det = Detector(efficiency=0.5, dark_count_rate=0, seed=42)
        n = 10_000
        photons = [
            PhotonPacket(basis=0, state=0, sequence_num=i) for i in range(n)
        ]
        events = det.detect_batch(photons)
        detected = sum(1 for e in events if e.detected)

        expected = n * 0.5
        sigma = np.sqrt(n * 0.5 * 0.5)
        assert abs(detected - expected) < 3 * sigma


class TestBasisMeasurement:
    """Test basis matching and random outcomes."""

    def test_matching_basis_deterministic(self):
        """When bases match, measured bit equals photon state."""
        det = Detector(efficiency=1.0, dark_count_rate=0, seed=42)

        correct = 0
        total = 0
        for _ in range(10_000):
            photon = PhotonPacket(basis=0, state=1, sequence_num=0)
            event = det.detect(photon)
            if event.basis == photon.basis and event.detected:
                total += 1
                if event.bit_value == photon.state:
                    correct += 1

        # All matching-basis measurements should give correct bit value
        assert total > 0
        assert correct == total

    def test_mismatched_basis_random(self):
        """When bases mismatch, bit value is random (≈50/50)."""
        det = Detector(efficiency=1.0, dark_count_rate=0, seed=42)

        values = []
        for i in range(10_000):
            # Alice sends Z-basis, force Bob to measure X-basis is random
            photon = PhotonPacket(basis=0, state=0, sequence_num=i)
            event = det.detect(photon)
            if event.basis != photon.basis and event.detected:
                values.append(event.bit_value)

        if len(values) > 100:
            frac_ones = sum(values) / len(values)
            # Should be roughly 0.5
            assert 0.4 < frac_ones < 0.6


class TestPolarizationError:
    """Test intrinsic QBER from polarization imperfection."""

    def test_no_error_when_fidelity_perfect(self):
        """polarization_error=0 → matched-basis bits are always correct."""
        det = Detector(efficiency=1.0, dark_count_rate=0, polarization_error=0.0, seed=7)
        errors = total = 0
        for i in range(10_000):
            photon = PhotonPacket(basis=0, state=1, sequence_num=i)
            event = det.detect(photon)
            if event.basis_match and event.detected:
                total += 1
                errors += event.bit_value != photon.state
        assert total > 0
        assert errors == 0

    def test_qber_matches_half_polarization_error(self):
        """Matched-basis QBER ≈ polarization_error / 2 (depolarizing model)."""
        p = 0.04  # → expected QBER ≈ 0.02
        det = Detector(efficiency=1.0, dark_count_rate=0, polarization_error=p, seed=7)
        errors = total = 0
        for i in range(40_000):
            photon = PhotonPacket(basis=0, state=1, sequence_num=i)
            event = det.detect(photon)
            if event.basis_match and event.detected:
                total += 1
                errors += event.bit_value != photon.state
        qber = errors / total
        expected = p / 2
        sigma = np.sqrt(expected * (1 - expected) / total)
        assert abs(qber - expected) < 4 * sigma


class TestDarkCounts:
    """Test dark count injection."""

    def test_dark_counts_occur(self):
        """High dark count rate → dark counts detected."""
        # Very high rate to ensure dark counts happen
        det = Detector(
            efficiency=0.0,  # No real detections
            dark_count_rate=1e9,  # 1 GHz → prob ≈ 1 per 1ns window
            detection_window=1e-9,
            seed=42,
        )
        photons = [
            PhotonPacket(basis=0, state=0, sequence_num=i) for i in range(1000)
        ]
        events = det.detect_batch(photons)
        dark = sum(1 for e in events if e.is_dark_count)
        assert dark > 0

    def test_no_dark_counts_zero_rate(self):
        """Zero dark count rate → no dark counts."""
        det = Detector(efficiency=0.0, dark_count_rate=0, seed=42)
        photons = [
            PhotonPacket(basis=0, state=0, sequence_num=i) for i in range(1000)
        ]
        events = det.detect_batch(photons)
        dark = sum(1 for e in events if e.is_dark_count)
        assert dark == 0


class TestDetectionEvent:
    """Test DetectionEvent data structure."""

    def test_fields(self):
        event = DetectionEvent(
            sequence_num=42,
            detected=True,
            basis=1,
            bit_value=0,
            is_dark_count=False,
            basis_match=True,
        )
        assert event.sequence_num == 42
        assert event.detected is True
        assert event.basis_match is True
