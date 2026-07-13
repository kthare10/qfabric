"""Entanglement swapping + repeater chains — physics validation (in-process).

Validates the Bell-state measurement (swap) op, the heralded Pauli correction, and
the end-to-end Werner-chain law F = (1 + 3·f^L)/4 that the ROADMAP names as the
Phase 4 acceptance criterion.
"""

from __future__ import annotations

import math

import pytest

from qne_sequence.qstate_core import QStateRegister
from qne_sequence.repeater import (
    chain_chsh,
    chain_fidelity,
    chain_qber,
    run_chain_session,
)

Z, X = 0.0, math.pi / 2


# -- Bell-state measurement (the swap op) -----------------------------------------

def test_bsm_heralds_identify_the_bell_state():
    # phi+ -> (0,0); X on one half -> psi+ (0,1); Z -> phi- (1,0); XZ -> psi- (1,1)
    for (x, z), want in [((0, 0), (0, 0)), ((1, 0), (0, 1)),
                         ((0, 1), (1, 0)), ((1, 1), (1, 1))]:
        reg = QStateRegister(seed=7)
        for _ in range(40):
            a, b = reg.create_bell_pair(1.0)
            reg.apply_pauli(b, x=x, z=z)
            assert reg.bell_measure(a, b) == want


def test_swap_with_correction_restores_phi_plus():
    # A-B1 and B2-C perfect pairs; BSM(B1,B2) + heralded X^m2 Z^m1 on C
    # leaves A-C perfectly correlated in BOTH Z and X — the phi+ signature.
    for angle in (Z, X):
        reg = QStateRegister(seed=11)
        n = 500
        for _ in range(n):
            a, b1 = reg.create_bell_pair(1.0)
            b2, c = reg.create_bell_pair(1.0)
            m1, m2 = reg.bell_measure(b1, b2)
            reg.apply_pauli(c, x=m2, z=m1)
            assert reg.measure(a, angle) == reg.measure(c, angle)


def test_bsm_outcomes_uniform_for_independent_pairs():
    reg = QStateRegister(seed=13)
    counts = {}
    n = 2000
    for _ in range(n):
        _, b1 = reg.create_bell_pair(1.0)
        b2, _ = reg.create_bell_pair(1.0)
        m = reg.bell_measure(b1, b2)
        counts[m] = counts.get(m, 0) + 1
    assert set(counts) == {(0, 0), (0, 1), (1, 0), (1, 1)}
    for c in counts.values():
        assert 0.18 < c / n < 0.32      # each outcome ~1/4

def test_bsm_requires_distinct_known_qubits():
    reg = QStateRegister(seed=17)
    a, b = reg.create_bell_pair(1.0)
    with pytest.raises(ValueError):
        reg.bell_measure(a, a)
    reg.measure(b, Z)
    with pytest.raises(KeyError):
        reg.bell_measure(a, b)          # b was consumed


# -- chain law: F_chain = (1 + 3 f^L)/4 -------------------------------------------

@pytest.mark.parametrize("nodes", [2, 3, 5])
def test_perfect_chain_has_zero_qber(nodes):
    r = run_chain_session(nodes, 800, fidelity=1.0, seed=21)
    assert r.delivered == 800
    assert r.qber == 0.0
    assert r.fidelity_est == 1.0
    assert r.swaps == (nodes - 2) * 800


@pytest.mark.parametrize("nodes,f", [(3, 0.95), (3, 0.9), (4, 0.9), (5, 0.95)])
def test_chain_qber_matches_werner_law(nodes, f):
    r = run_chain_session(nodes, 8000, fidelity=f, seed=23)
    predicted = chain_qber(f, nodes - 1)            # (1 - f^L)/2
    assert r.qber_pred == pytest.approx(predicted)
    assert abs(r.qber - predicted) < 0.015
    assert abs(r.fidelity_est - chain_fidelity(f, nodes - 1)) < 0.025


def test_chain_chsh_degrades_as_w_to_the_L():
    # 3 nodes at f=0.9: S = 2sqrt(2)*0.81 ~ 2.29 — still a Bell violation
    r = run_chain_session(3, 6000, fidelity=0.9, mode="chsh", seed=29)
    assert abs(r.chsh_s - chain_chsh(0.9, 2)) < 0.2
    assert r.chsh_s > 2.0
    # 4 nodes at f=0.7: S ~ 0.97 — swapping noisy links destroys the violation
    r2 = run_chain_session(4, 6000, fidelity=0.7, mode="chsh", seed=31)
    assert abs(r2.chsh_s - chain_chsh(0.7, 3)) < 0.2
    assert r2.chsh_s < 2.0


def test_without_heralded_correction_pair_is_useless():
    # skipping the Pauli fix-up leaves an even Bell mixture: QBER -> 1/2
    r = run_chain_session(3, 4000, fidelity=1.0, apply_correction=False, seed=37)
    assert abs(r.qber - 0.5) < 0.04
    assert not r.corrected


def test_per_link_loss_gates_delivery():
    r = run_chain_session(3, 4000, fidelity=1.0, loss_probability=0.2, seed=41)
    assert abs(r.delivered / r.attempts - 0.8 ** 2) < 0.03   # both links must survive
    assert r.qber == 0.0                                     # loss heralds, not corrupts


def test_heralds_recorded_per_swap():
    r = run_chain_session(4, 1000, fidelity=1.0, seed=43)
    assert sum(r.heralds.values()) == r.swaps == 2 * 1000
