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

"""Eavesdropper models for BB84 — demonstrating the security claim.

BB84's whole point is that eavesdropping is *detectable*: an attacker who measures
photons in transit disturbs them, raising the QBER. Nothing in the protocol path
exercised that until now — measured QBER only reflected channel noise. This module
adds a configurable Eve so a run can show QBER climb and the secure key rate
collapse past the ~11% threshold.

Intercept-resend attack: for a fraction f of photons, Eve measures in a randomly
chosen basis (she doesn't know Alice's) and resends a fresh photon prepared from her
result. On the *sifted* key (where Alice's and Bob's bases agree):

  * Eve guessed the right basis (prob 1/2): she learns and resends the bit correctly
    → no error.
  * Eve guessed wrong (prob 1/2): she resends in the wrong basis, so Bob's outcome is
    random → error with prob 1/2.

So a fully-tapped channel (f = 1) adds QBER = 1/2 · 1/2 = 0.25 — far above the ~11%
security threshold — and tapping a fraction f adds QBER ≈ 0.25·f (on top of the
channel's intrinsic QBER). ``expected_sifted_qber`` returns that prediction.

Eve is a per-photon transform on the *physical* (basis, bit) the receiver sees;
sifting still uses Alice's separately-announced basis, so the disturbance shows up
exactly as the theory predicts.
"""

from __future__ import annotations

import numpy as np


def expected_sifted_qber(intercept_fraction: float) -> float:
    """QBER an intercept-resend Eve adds to the sifted key: 0.25 · f."""
    return 0.25 * max(0.0, min(1.0, intercept_fraction))


class InterceptResendEve:
    """Intercept-resend eavesdropper on the quantum channel.

    Args:
        intercept_fraction: probability f in [0, 1] that any given photon is tapped.
        seed: RNG seed for reproducible basis/bit choices.
    """

    def __init__(self, intercept_fraction: float = 1.0, seed: int = 0):
        self.f = max(0.0, min(1.0, float(intercept_fraction)))
        self.rng = np.random.default_rng(seed)
        self.photons_seen = 0
        self.photons_intercepted = 0
        self.eve_basis_match = 0     # times Eve's random basis matched Alice's

    def intercept(self, basis: int, bit: int) -> tuple[int, int]:
        """Return the (basis, bit) of the photon that actually reaches Bob.

        With prob f, Eve measures in a random basis and resends her outcome; with
        prob 1−f the photon passes through untouched.
        """
        self.photons_seen += 1
        if self.f <= 0.0 or self.rng.random() >= self.f:
            return int(basis), int(bit)
        self.photons_intercepted += 1
        e_basis = int(self.rng.integers(0, 2))
        if e_basis == int(basis):
            self.eve_basis_match += 1
            e_bit = int(bit)                       # right basis → learns the bit
        else:
            e_bit = int(self.rng.integers(0, 2))   # wrong basis → random resend
        return e_basis, e_bit

    def intercept_pulses(self, pulses):
        """Transform a list of [seq, basis, bit] pulses; returns a new list."""
        return [[seq, *self.intercept(basis, bit)] for seq, basis, bit in pulses]

    @property
    def stats(self) -> dict:
        return {
            "eve_intercept_fraction": self.f,
            "eve_photons_seen": self.photons_seen,
            "eve_photons_intercepted": self.photons_intercepted,
            "eve_basis_match": self.eve_basis_match,
        }
