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

"""BB84 QKD protocol logic: sifting, QBER estimation, and key rate.

Implements the classical post-processing steps of BB84:
1. Basis sifting — keep only bits where Alice and Bob used the same basis
2. QBER estimation — sample a fraction of sifted bits to estimate error rate
3. Secure key rate — Shor-Preskill bound for asymptotic key rate
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass
class SiftingResult:
    """Result of BB84 basis sifting."""
    alice_bits: list[int]
    bob_bits: list[int]
    matching_indices: list[int]  # Original sequence numbers that matched
    sifted_count: int = 0

    def __post_init__(self):
        self.sifted_count = len(self.alice_bits)


@dataclass
class QBEREstimate:
    """QBER estimation from sampled sifted bits."""
    qber: float
    num_sampled: int
    num_errors: int
    confidence_interval: tuple[float, float] = (0.0, 0.0)


@dataclass
class KeyRateResult:
    """Secure key rate calculation result."""
    raw_key_rate: float       # Sifted bits per photon sent
    secure_key_rate: float    # Secure bits per photon sent (after privacy amp)
    sifted_bits: int
    final_key_bits: int
    qber: float


@dataclass
class AliceRecord:
    """Alice's record of a sent photon."""
    sequence_num: int
    basis: int
    bit_value: int


@dataclass
class BobRecord:
    """Bob's record of a detected photon."""
    sequence_num: int
    basis: int
    bit_value: int


class BB84Protocol:
    """BB84 QKD classical post-processing."""

    def __init__(self, sample_fraction: float = 0.1, seed: Optional[int] = None):
        """
        Args:
            sample_fraction: Fraction of sifted bits to sample for QBER estimation.
            seed: Random seed for reproducible sampling.
        """
        self.sample_fraction = sample_fraction
        self.rng = np.random.default_rng(seed)

    def sift(
        self,
        alice_log: list[AliceRecord],
        bob_log: list[BobRecord],
    ) -> SiftingResult:
        """Perform basis sifting between Alice and Bob.

        Keeps only bits where both parties used the same basis AND
        Bob actually detected the photon (Bob's log only contains detections).
        """
        # Index Bob's records by sequence number for fast lookup
        bob_by_seq = {r.sequence_num: r for r in bob_log}

        alice_bits = []
        bob_bits = []
        matching_indices = []

        for alice_rec in alice_log:
            bob_rec = bob_by_seq.get(alice_rec.sequence_num)
            if bob_rec is None:
                continue  # Photon lost or not detected
            if alice_rec.basis == bob_rec.basis:
                alice_bits.append(alice_rec.bit_value)
                bob_bits.append(bob_rec.bit_value)
                matching_indices.append(alice_rec.sequence_num)

        return SiftingResult(
            alice_bits=alice_bits,
            bob_bits=bob_bits,
            matching_indices=matching_indices,
        )

    def estimate_qber(self, sifted: SiftingResult) -> QBEREstimate:
        """Estimate QBER by sampling a fraction of sifted bits.

        The sampled bits are consumed (removed from the key material).
        """
        n = sifted.sifted_count
        if n == 0:
            return QBEREstimate(qber=0.0, num_sampled=0, num_errors=0)

        num_sample = max(1, int(n * self.sample_fraction))
        sample_indices = self.rng.choice(n, size=num_sample, replace=False)

        errors = 0
        for idx in sample_indices:
            if sifted.alice_bits[idx] != sifted.bob_bits[idx]:
                errors += 1

        qber = errors / num_sample if num_sample > 0 else 0.0

        # Wilson score confidence interval
        if num_sample > 0:
            z = 1.96  # 95% CI
            denominator = 1 + z**2 / num_sample
            center = (qber + z**2 / (2 * num_sample)) / denominator
            spread = z * np.sqrt(
                (qber * (1 - qber) + z**2 / (4 * num_sample)) / num_sample
            ) / denominator
            ci = (max(0.0, center - spread), min(1.0, center + spread))
        else:
            ci = (0.0, 0.0)

        return QBEREstimate(
            qber=qber,
            num_sampled=num_sample,
            num_errors=errors,
            confidence_interval=ci,
        )

    @staticmethod
    def binary_entropy(p: float) -> float:
        """Binary entropy function H(p)."""
        if p <= 0.0 or p >= 1.0:
            return 0.0
        return -p * np.log2(p) - (1 - p) * np.log2(1 - p)

    @staticmethod
    def secure_key_fraction(qber: float) -> float:
        """Asymptotic Shor-Preskill secure fraction per sifted bit: 1 - 2*H(QBER).

        Returns 0 above the ~11% BB84 security threshold (or for invalid QBER).
        Single source of truth for the secure-key-rate math used by the sim path,
        the live Bob path, and the simulator adapters.
        """
        if 0.0 <= qber < 0.11:
            return max(0.0, 1.0 - 2.0 * BB84Protocol.binary_entropy(qber))
        return 0.0

    def compute_key_rate(
        self,
        sifted: SiftingResult,
        qber_estimate: QBEREstimate,
        num_photons_sent: int,
    ) -> KeyRateResult:
        """Compute secure key rate using Shor-Preskill bound.

        Asymptotic secure key rate per sifted bit:
            r = 1 - 2*H(QBER)

        where H is the binary entropy function.
        """
        qber = qber_estimate.qber
        n_sifted = sifted.sifted_count
        n_remaining = n_sifted - qber_estimate.num_sampled

        secure_rate_per_sifted = self.secure_key_fraction(qber)

        final_key_bits = int(n_remaining * secure_rate_per_sifted)
        raw_key_rate = n_sifted / num_photons_sent if num_photons_sent > 0 else 0.0
        secure_key_rate = final_key_bits / num_photons_sent if num_photons_sent > 0 else 0.0

        return KeyRateResult(
            raw_key_rate=raw_key_rate,
            secure_key_rate=secure_key_rate,
            sifted_bits=n_sifted,
            final_key_bits=final_key_bits,
            qber=qber,
        )
