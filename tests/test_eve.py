# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Komal Thareja
#
# Author: Komal Thareja (kthare10@renci.org)

"""Intercept-resend eavesdropper — the BB84 security demonstration.

Pins the canonical result: a full intercept-resend attack adds ~25% QBER to the
sifted key, and tapping a fraction f adds ~0.25*f — enough to push past the ~11%
security threshold and collapse the secure key rate.
"""

from __future__ import annotations

import numpy as np

from qne.bb84 import AliceRecord, BB84Protocol, BobRecord
from qne.detector import Detector
from qne.eve import InterceptResendEve, expected_sifted_qber


def _sifted_qber_with_eve(f, n=20000, seed=0):
    """Run a minimal prepare-measure BB84 through Eve + the detector; return the
    QBER on the sifted key (bases agree). Ideal detector, no channel noise, so any
    error is Eve's disturbance."""
    rng = np.random.default_rng(seed)
    eve = InterceptResendEve(intercept_fraction=f, seed=seed + 1)
    det = Detector(efficiency=1.0, dark_count_rate=0.0, polarization_error=0.0,
                   seed=seed + 2)
    alice, bob = [], []
    for i in range(n):
        a_basis = int(rng.integers(0, 2))
        a_bit = int(rng.integers(0, 2))
        # Eve intercepts in transit; Bob's detector then measures her resent photon
        e_basis, e_bit = eve.intercept(a_basis, a_bit)
        import types
        ev = det.detect(types.SimpleNamespace(basis=e_basis, state=e_bit,
                                              sequence_num=i))
        if ev.detected:
            alice.append(AliceRecord(i, a_basis, a_bit))   # Alice announces her TRUE basis
            bob.append(BobRecord(i, ev.basis, ev.bit_value))
    sifted = BB84Protocol().sift(alice, bob)
    if sifted.sifted_count == 0:
        return 0.0
    errors = sum(a != b for a, b in zip(sifted.alice_bits, sifted.bob_bits))
    return errors / sifted.sifted_count


def test_expected_sifted_qber_formula():
    assert expected_sifted_qber(0.0) == 0.0
    assert expected_sifted_qber(1.0) == 0.25
    assert abs(expected_sifted_qber(0.5) - 0.125) < 1e-12


def test_no_eavesdropper_no_errors():
    assert _sifted_qber_with_eve(f=0.0) < 0.01


def test_full_intercept_gives_25pct_qber():
    q = _sifted_qber_with_eve(f=1.0)
    assert abs(q - 0.25) < 0.02, q


def test_partial_intercept_scales_linearly():
    for f in (0.25, 0.5, 0.75):
        q = _sifted_qber_with_eve(f=f)
        assert abs(q - 0.25 * f) < 0.02, (f, q)


def test_full_intercept_breaks_security():
    """QBER ~25% is well above the ~11% threshold -> zero secure fraction."""
    q = _sifted_qber_with_eve(f=1.0)
    assert BB84Protocol.secure_key_fraction(q) == 0.0


def test_eve_basis_match_rate_is_half():
    eve = InterceptResendEve(intercept_fraction=1.0, seed=7)
    for i in range(20000):
        eve.intercept(int(i % 2), int((i // 2) % 2))
    assert eve.photons_intercepted == 20000
    # Eve's random basis matches Alice's about half the time
    assert abs(eve.eve_basis_match / eve.photons_intercepted - 0.5) < 0.02


def test_untapped_photons_pass_through_unchanged():
    eve = InterceptResendEve(intercept_fraction=0.0, seed=1)
    for basis in (0, 1):
        for bit in (0, 1):
            assert eve.intercept(basis, bit) == (basis, bit)
    assert eve.photons_intercepted == 0
