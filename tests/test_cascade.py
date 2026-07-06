# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Komal Thareja
#
# Author: Komal Thareja (kthare10@renci.org)

"""Cascade error reconciliation — correctness and leakage."""

from __future__ import annotations

from functools import reduce
from operator import xor

import numpy as np

from qne.cascade import initial_block_size, leak_efficiency, reconcile


def _oracle(alice_key):
    """In-process parity oracle: Alice's parity over each index list."""
    return lambda blocks: [reduce(xor, (alice_key[i] for i in b), 0) for b in blocks]


def _noisy_pair(n, qber, seed):
    rng = np.random.default_rng(seed)
    alice = list(rng.integers(0, 2, n))
    bob = [b ^ (1 if rng.random() < qber else 0) for b in alice]
    return alice, bob


def test_initial_block_size():
    assert initial_block_size(0.0, 1000) == 1000       # no errors -> one block
    assert initial_block_size(0.073, 1000) == 10       # 0.73/0.073
    assert initial_block_size(0.01, 50) == 50          # clamped to n


def test_zero_qber_is_noop():
    alice, bob = _noisy_pair(500, 0.0, seed=1)
    assert alice == bob
    r = reconcile(bob, _oracle(alice), 0.0, seed=1)
    assert r.corrected_key == alice
    assert r.corrections == 0


def test_reconciles_perfectly_across_qber():
    """The whole point: corrected key exactly equals Alice's, every trial."""
    n = 2000
    for qber in (0.01, 0.03, 0.05, 0.08, 0.12):
        for t in range(12):
            alice, bob = _noisy_pair(n, qber, seed=100 + t)
            r = reconcile(bob, _oracle(alice), qber, passes=4, seed=7 + t)
            assert r.corrected_key == alice, f"residual at QBER={qber}, trial {t}"


def test_corrections_match_injected_errors_at_low_qber():
    alice, bob = _noisy_pair(3000, 0.02, seed=3)
    injected = sum(a != b for a, b in zip(alice, bob))
    r = reconcile(bob, _oracle(alice), 0.02, seed=3)
    assert r.corrected_key == alice
    # each real error costs at least one correction; cascade may add a few
    assert r.corrections >= injected


def test_leakage_is_bounded_and_efficient():
    n = 4000
    alice, bob = _noisy_pair(n, 0.05, seed=4)
    r = reconcile(bob, _oracle(alice), 0.05, seed=4)
    assert r.corrected_key == alice
    # Cascade leaks a bit above the Shannon limit n*H(Q): efficiency ~1.0-1.5
    f = leak_efficiency(r.bits_leaked, n, 0.05)
    assert 1.0 <= f <= 1.6
    assert r.bits_leaked < n           # never leak the whole key at low QBER


def test_leak_efficiency_edge_cases():
    assert leak_efficiency(100, 0, 0.05) is None
    assert leak_efficiency(100, 1000, 0.0) is None
    assert leak_efficiency(100, 1000, 1.0) is None


def test_empty_key():
    r = reconcile([], _oracle([]), 0.05)
    assert r.corrected_key == []
    assert r.corrections == 0 and r.bits_leaked == 0
