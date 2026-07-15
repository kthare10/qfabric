"""Proof of concept: SeQUeNCe's QuantumManager can back qfabric's register.

Validates SeQUeNCeRegister (backed by sequence.kernel QuantumManagerKet) reproduces
the same entanglement physics as the numpy QStateRegister — the concrete evidence
that RemoteQuantumManager could run on SeQUeNCe's quantum layer.
"""

from __future__ import annotations

import math

import pytest

from qne_sequence.qstate_sequence import SeQUeNCeRegister

Z, X = 0.0, math.pi / 2


@pytest.mark.parametrize("angle", [Z, X, math.pi / 4])
def test_matching_basis_perfectly_correlated_at_f1(angle):
    reg = SeQUeNCeRegister(seed=1)
    eq = n = 800
    got = sum(reg.measure(a, angle) == reg.measure(b, angle)
              for a, b in (reg.create_bell_pair(1.0) for _ in range(n)))
    assert got == eq   # ideal Bell pair: matching-basis outcomes always equal


@pytest.mark.parametrize("f,exp", [(1.0, 0.0), (0.9, 0.05), (0.8, 0.10)])
def test_qber_equals_one_minus_f_over_two(f, exp):
    reg = SeQUeNCeRegister(seed=2)
    err = 0
    n = 8000
    for i in range(n):
        a, b = reg.create_bell_pair(f)
        ang = Z if i % 2 == 0 else X
        err += reg.measure(a, ang) != reg.measure(b, ang)
    assert abs(err / n - exp) < 0.02


def test_chsh_reaches_tsirelson_at_f1():
    reg = SeQUeNCeRegister(seed=3)

    def E(a, b, n=2000):
        s = 0
        for _ in range(n):
            ka, kb = reg.create_bell_pair(1.0)
            s += 1 if reg.measure(ka, a) == reg.measure(kb, b) else -1
        return s / n

    a0, a1, b0, b1 = 0.0, math.pi / 2, math.pi / 4, 3 * math.pi / 4
    S = abs(E(a0, b0) - E(a0, b1) + E(a1, b0) + E(a1, b1))
    assert S > 2.0                       # Bell violation
    assert abs(S - 2 * math.sqrt(2)) < 0.15


def test_bell_measure_heralds_identify_state():
    # phi+ -> (0,0); X on one half -> psi+ (0,1); Z -> phi- (1,0); XZ -> psi- (1,1)
    for (x, z), want in [((0, 0), (0, 0)), ((1, 0), (0, 1)),
                         ((0, 1), (1, 0)), ((1, 1), (1, 1))]:
        reg = SeQUeNCeRegister(seed=7)
        for _ in range(30):
            a, b = reg.create_bell_pair(1.0)
            reg.apply_pauli(b, x=x, z=z)
            assert reg.bell_measure(a, b) == want


def test_swap_with_correction_restores_phi_plus():
    # A-R1, R2-B perfect pairs; BSM(R1,R2) + heralded X^m2 Z^m1 on B -> A,B correlated
    for angle in (Z, X):
        reg = SeQUeNCeRegister(seed=11)
        n = 400
        for _ in range(n):
            a, r1 = reg.create_bell_pair(1.0)
            r2, b = reg.create_bell_pair(1.0)
            m1, m2 = reg.bell_measure(r1, r2)
            reg.apply_pauli(b, x=m2, z=m1)
            assert reg.measure(a, angle) == reg.measure(b, angle)
