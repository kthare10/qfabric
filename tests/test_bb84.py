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

"""Tests for BB84 sifting logic and key rate computation."""


from qne.bb84 import AliceRecord, BB84Protocol, BobRecord, SiftingResult


class TestSifting:
    """Test BB84 basis sifting."""

    def test_perfect_sifting(self):
        """All bases match, all photons detected → 100% sift rate."""
        alice_log = [
            AliceRecord(sequence_num=i, basis=i % 2, bit_value=i % 2)
            for i in range(100)
        ]
        # Bob detects all and uses same bases
        bob_log = [
            BobRecord(sequence_num=i, basis=i % 2, bit_value=i % 2)
            for i in range(100)
        ]

        protocol = BB84Protocol(sample_fraction=0.1, seed=42)
        result = protocol.sift(alice_log, bob_log)

        assert result.sifted_count == 100
        assert result.alice_bits == result.bob_bits

    def test_half_basis_match(self):
        """Alice uses all Z, Bob alternates Z/X → ~50% sift rate."""
        alice_log = [
            AliceRecord(sequence_num=i, basis=0, bit_value=0) for i in range(100)
        ]
        bob_log = [
            BobRecord(sequence_num=i, basis=i % 2, bit_value=0)
            for i in range(100)
        ]

        protocol = BB84Protocol(seed=42)
        result = protocol.sift(alice_log, bob_log)

        # Only even-numbered photons match (Bob basis=0)
        assert result.sifted_count == 50

    def test_lost_photons(self):
        """Bob only detects half the photons."""
        alice_log = [
            AliceRecord(sequence_num=i, basis=0, bit_value=0) for i in range(100)
        ]
        # Bob only detects even-numbered photons, same basis
        bob_log = [
            BobRecord(sequence_num=i, basis=0, bit_value=0)
            for i in range(0, 100, 2)
        ]

        protocol = BB84Protocol(seed=42)
        result = protocol.sift(alice_log, bob_log)

        assert result.sifted_count == 50

    def test_no_detections(self):
        """Bob detects nothing → empty sifting result."""
        alice_log = [
            AliceRecord(sequence_num=i, basis=0, bit_value=0) for i in range(100)
        ]
        bob_log = []

        protocol = BB84Protocol(seed=42)
        result = protocol.sift(alice_log, bob_log)

        assert result.sifted_count == 0


class TestQBER:
    """Test QBER estimation."""

    def test_zero_qber(self):
        """Perfect channel → QBER = 0."""
        sifted = SiftingResult(
            alice_bits=[0, 1, 0, 1, 1, 0, 0, 1, 1, 0],
            bob_bits=[0, 1, 0, 1, 1, 0, 0, 1, 1, 0],
            matching_indices=list(range(10)),
        )
        protocol = BB84Protocol(sample_fraction=1.0, seed=42)
        estimate = protocol.estimate_qber(sifted)
        assert estimate.qber == 0.0
        assert estimate.num_errors == 0

    def test_known_qber(self):
        """Inject known errors → QBER matches."""
        n = 1000
        alice_bits = [0] * n
        bob_bits = [0] * n
        # Flip 10% of bits
        for i in range(0, n, 10):
            bob_bits[i] = 1

        sifted = SiftingResult(
            alice_bits=alice_bits,
            bob_bits=bob_bits,
            matching_indices=list(range(n)),
        )
        protocol = BB84Protocol(sample_fraction=1.0, seed=42)
        estimate = protocol.estimate_qber(sifted)

        assert abs(estimate.qber - 0.10) < 0.01

    def test_empty_sifted(self):
        """No sifted bits → QBER = 0."""
        sifted = SiftingResult(alice_bits=[], bob_bits=[], matching_indices=[])
        protocol = BB84Protocol(seed=42)
        estimate = protocol.estimate_qber(sifted)
        assert estimate.qber == 0.0
        assert estimate.num_sampled == 0


class TestKeyRate:
    """Test secure key rate computation."""

    def test_zero_qber_positive_rate(self):
        """Zero QBER → maximum key rate."""
        sifted = SiftingResult(
            alice_bits=[0] * 100,
            bob_bits=[0] * 100,
            matching_indices=list(range(100)),
        )
        protocol = BB84Protocol(sample_fraction=0.1, seed=42)
        qber_est = protocol.estimate_qber(sifted)
        result = protocol.compute_key_rate(sifted, qber_est, num_photons_sent=200)

        assert result.qber == 0.0
        assert result.secure_key_rate > 0
        assert result.raw_key_rate == 0.5  # 100 sifted / 200 sent

    def test_high_qber_zero_rate(self):
        """QBER > 11% → zero secure key rate."""
        n = 100
        alice_bits = [0] * n
        bob_bits = [0] * n
        # Flip 15% → above BB84 threshold
        for i in range(15):
            bob_bits[i] = 1

        sifted = SiftingResult(
            alice_bits=alice_bits,
            bob_bits=bob_bits,
            matching_indices=list(range(n)),
        )
        protocol = BB84Protocol(sample_fraction=1.0, seed=42)
        qber_est = protocol.estimate_qber(sifted)
        result = protocol.compute_key_rate(sifted, qber_est, num_photons_sent=200)

        assert result.secure_key_rate == 0.0
        assert result.final_key_bits == 0

    def test_binary_entropy(self):
        """Binary entropy boundary values."""
        assert BB84Protocol.binary_entropy(0.0) == 0.0
        assert BB84Protocol.binary_entropy(1.0) == 0.0
        assert abs(BB84Protocol.binary_entropy(0.5) - 1.0) < 1e-10

    def test_secure_key_fraction(self):
        """Shared Shor-Preskill secure fraction (single source of truth)."""
        skf = BB84Protocol.secure_key_fraction
        assert skf(0.0) == 1.0                      # noiseless → full fraction
        assert skf(0.11) == 0.0                     # at/above the security threshold
        assert skf(0.5) == 0.0
        assert skf(-0.1) == 0.0                     # invalid QBER → 0
        # Monotonically decreasing on [0, 0.11)
        assert skf(0.01) > skf(0.05) > skf(0.10) > 0.0
        # Matches the closed form for a mid value
        q = 0.02
        assert abs(skf(q) - (1.0 - 2.0 * BB84Protocol.binary_entropy(q))) < 1e-12
