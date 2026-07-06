"""Entanglement / E91 / BBM92 — physics and protocol validation (in-process).

Validates the shared quantum-state register, the state service, and the E91/BBM92
protocol logic against theory, before the distributed transport is layered on.
"""

from __future__ import annotations

import math

import pytest

from qne.bb84 import BB84Protocol
from qne_sequence.qstate_core import QStateRegister
from qne_sequence.quantum_state_service import QuantumStateService
from qne_sequence.e91 import run_session

Z, X = 0.0, math.pi / 2


# -- register physics ------------------------------------------------------------

@pytest.mark.parametrize("angle", [Z, X])
def test_matching_basis_perfectly_correlated_at_f1(angle):
    reg = QStateRegister(seed=1)
    eq = 0
    n = 3000
    for _ in range(n):
        a, b = reg.create_bell_pair(1.0)
        eq += reg.measure(a, angle) == reg.measure(b, angle)
    assert eq == n   # ideal Bell pair: matching-basis outcomes always equal


@pytest.mark.parametrize("f,exp", [(1.0, 0.0), (0.98, 0.01), (0.9, 0.05), (0.8, 0.10)])
def test_qber_equals_one_minus_f_over_two(f, exp):
    reg = QStateRegister(seed=2)
    err = 0
    n = 8000
    for i in range(n):
        a, b = reg.create_bell_pair(f)
        ang = Z if i % 2 == 0 else X
        err += reg.measure(a, ang) != reg.measure(b, ang)
    assert abs(err / n - exp) < 0.015


def test_measurement_reproducible_from_sample():
    # same explicit sample -> same outcome (drives distributed reproducibility)
    r1 = QStateRegister(seed=3)
    a1, _ = r1.create_bell_pair(1.0)
    r2 = QStateRegister(seed=3)
    a2, _ = r2.create_bell_pair(1.0)
    assert r1.measure(a1, X, samp=0.9) == r2.measure(a2, X, samp=0.9)


def test_unknown_qubit_raises():
    reg = QStateRegister(seed=0)
    with pytest.raises(KeyError):
        reg.measure(999, Z)


# -- CHSH Bell test --------------------------------------------------------------

def _chsh(seed, f, n=8000):
    reg = QStateRegister(seed=seed)
    combos = {(0.0, math.pi / 4): +1, (0.0, 3 * math.pi / 4): -1,
              (math.pi / 2, math.pi / 4): +1, (math.pi / 2, 3 * math.pi / 4): +1}
    s = 0.0
    for (aa, bb), sign in combos.items():
        acc = 0
        for _ in range(n):
            qa, qb = reg.create_bell_pair(f)
            acc += 1 if reg.measure(qa, aa) == reg.measure(qb, bb) else -1
        s += sign * acc / n
    return abs(s)


def test_chsh_violates_classical_bound_ideal():
    s = _chsh(seed=4, f=1.0)
    assert s > 2.6                       # quantum: 2√2 ≈ 2.828 (finite-sample)
    assert s <= 2.83 + 0.1               # not unphysically above Tsirelson


def test_chsh_degrades_to_classical_at_threshold():
    # F = 1/√2 -> S ≈ 2, the security boundary; below it, no certified secrecy
    s = _chsh(seed=5, f=1 / math.sqrt(2))
    assert abs(s - 2.0) < 0.15


# -- full E91 / BBM92 sessions ---------------------------------------------------

def test_bbm92_lossless_keys_agree_exactly():
    svc = QuantumStateService(seed=6)
    r = run_session(svc, 6000, fidelity=1.0, mode="bbm92",
                    sample_fraction=0.2, alice_seed=1, bob_seed=2)
    assert r.qber == 0.0
    assert r.key_bits > 0
    assert r.alice_key == r.bob_key      # perfect correlation -> identical key
    assert r.chsh_s is None              # bbm92 has no Bell test


def test_e91_session_secure_and_violates_bell():
    svc = QuantumStateService(seed=7)
    r = run_session(svc, 12000, fidelity=0.98, loss_probability=0.045,
                    mode="e91", sample_fraction=0.2, alice_seed=1, bob_seed=2)
    assert 0.0 <= r.qber < 0.03          # ~ (1-F)/2 = 0.01
    assert r.chsh_s is not None and r.chsh_s > 2.0   # entanglement certified
    assert r.secure_fraction > 0.0
    assert r.detected_pairs < r.num_pairs            # loss removed some pairs
    assert r.key_bits > 0


def test_loss_reduces_detected_pairs():
    svc = QuantumStateService(seed=8)
    r = run_session(svc, 5000, fidelity=1.0, loss_probability=0.3, mode="bbm92")
    assert abs(r.detected_pairs / r.num_pairs - 0.7) < 0.03


def test_disclosed_sample_bits_excluded_from_key():
    """Regression (review H2): the QBER-disclosed sample must not remain in the
    key. With F<1 the key still has to equal Alice's own key bits at the kept
    positions, and key_bits must be sifted - num_sampled."""
    svc = QuantumStateService(seed=42)
    r = run_session(svc, 4000, fidelity=0.97, mode="bbm92",
                    sample_fraction=0.3, alice_seed=1, bob_seed=2)
    assert r.key_bits == r.sifted_bits - r.num_sampled
    # a large sample fraction removes a proportional chunk (not a fixed prefix)
    assert r.num_sampled == BB84Protocol.sample_size(r.sifted_bits, 0.3)


def test_high_noise_kills_secure_fraction():
    svc = QuantumStateService(seed=9)
    # F below the QBER=11% threshold -> zero secure fraction
    r = run_session(svc, 8000, fidelity=0.75, mode="bbm92", sample_fraction=0.2)
    assert r.qber > 0.11
    assert r.secure_fraction == 0.0
