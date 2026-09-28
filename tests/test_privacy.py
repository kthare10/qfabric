# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Komal Thareja
#
# Author: Komal Thareja (kthare10@renci.org)

"""Privacy amplification — the Toeplitz-hash secret-key extractor."""

from __future__ import annotations

import numpy as np

from qne.privacy import toeplitz_amplify, toeplitz_amplify_dense


def _key(n, seed=0):
    return list(np.random.default_rng(seed).integers(0, 2, n))


def test_output_length_and_clamping():
    k = _key(1000)
    assert len(toeplitz_amplify(k, 400, seed=1)) == 400
    assert len(toeplitz_amplify(k, 1000, seed=1)) == 1000
    assert len(toeplitz_amplify(k, 5000, seed=1)) == 1000   # clamped to len(key)


def test_deterministic_for_a_given_seed():
    k = _key(2000)
    assert toeplitz_amplify(k, 800, seed=42) == toeplitz_amplify(k, 800, seed=42)


def test_both_parties_get_the_same_secret():
    """Alice and Bob hold the identical reconciled key + same public hash → same output."""
    alice = _key(3000, seed=5)
    bob = list(alice)                       # identical after reconciliation
    assert toeplitz_amplify(alice, 1200, seed=99) == toeplitz_amplify(bob, 1200, seed=99)


def test_avalanche_one_bit_flip_changes_half_the_output():
    k = _key(4000, seed=3)
    flipped = list(k)
    flipped[137] ^= 1
    a = toeplitz_amplify(k, 1500, seed=7)
    b = toeplitz_amplify(flipped, 1500, seed=7)
    frac = sum(x != y for x, y in zip(a, b)) / len(a)
    assert 0.4 < frac < 0.6                 # 2-universal hash → ~50% of bits flip


def test_different_seed_gives_different_hash():
    k = _key(2000, seed=2)
    assert toeplitz_amplify(k, 800, seed=1) != toeplitz_amplify(k, 800, seed=2)


def test_output_is_balanced():
    k = _key(4000, seed=8)
    out = toeplitz_amplify(k, 2000, seed=4)
    assert 0.45 < np.mean(out) < 0.55


def test_edge_cases():
    assert toeplitz_amplify(_key(100), 0, seed=1) == []
    assert toeplitz_amplify([], 10, seed=1) == []
    assert set(toeplitz_amplify(_key(500), 200, seed=1)) <= {0, 1}


def test_fft_hash_is_bit_identical_to_the_dense_matrix():
    """The matrix-free hash must be the SAME 2-universal hash as the dense product
    (same seed -> same diagonal bits -> same output), for odd sizes and edges too."""
    for n, m, seed in [(1, 1, 0), (2, 1, 1), (7, 3, 2), (64, 64, 3), (1000, 400, 4),
                       (1001, 999, 5), (3000, 1200, 99), (4097, 2048, 6)]:
        k = _key(n, seed=seed + 100)
        assert toeplitz_amplify(k, m, seed=seed) == toeplitz_amplify_dense(k, m, seed=seed), (n, m)


def test_large_sifted_key_does_not_need_a_dense_matrix():
    """A 1 km, 100k-photon slice run hands ~38k sifted bits to PA; the dense matrix
    (~12 GB) OOM-killed both nodes on 2026-09-28. Must run in well under a second
    with O(n) memory."""
    import time
    k = _key(38000, seed=11)
    t0 = time.perf_counter()
    out = toeplitz_amplify(k, 20000, seed=12)
    assert len(out) == 20000 and set(out) <= {0, 1}
    assert time.perf_counter() - t0 < 2.0
    # still balanced and seed-sensitive at this size
    assert abs(sum(out) / len(out) - 0.5) < 0.02
    assert toeplitz_amplify(k, 20000, seed=13) != out
