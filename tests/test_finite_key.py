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

"""Finite-key bound — sanity, monotonicity, and convergence to the asymptote."""

from __future__ import annotations

from qne.bb84 import BB84Protocol
from qne.finite_key import finite_key_length, planned_leak, serfling_mu


def _leak(n, q):
    return planned_leak(n, q)


def test_finite_never_exceeds_asymptotic():
    for n, k, q in [(10_000, 1_000, 0.02), (100_000, 10_000, 0.05), (2_000, 200, 0.01)]:
        r = finite_key_length(n, k, q, _leak(n, q))
        assert r.secret_bits <= r.asymptotic_bits


def test_qber_upper_exceeds_observed_and_caps():
    r = finite_key_length(10_000, 1_000, 0.03, _leak(10_000, 0.03))
    assert r.qber_upper > 0.03
    r2 = finite_key_length(100, 10, 0.49, _leak(100, 0.49))
    assert r2.qber_upper == 0.5
    assert r2.secret_bits == 0


def test_small_runs_yield_no_key():
    # the log(eps) terms alone are ~115 bits; a few hundred noisy bits can't pay
    r = finite_key_length(400, 40, 0.05, _leak(400, 0.05))
    assert r.secret_bits == 0


def test_larger_sample_tightens_the_bound():
    n, q = 50_000, 0.03
    small = finite_key_length(n, 500, q, _leak(n, q))
    large = finite_key_length(n, 5_000, q, _leak(n, q))
    assert large.mu < small.mu
    assert large.secret_bits > small.secret_bits


def test_converges_to_asymptotic_rate_with_n():
    q = 0.02
    rates = []
    for n in (5_000, 50_000, 500_000):
        k = n // 10
        r = finite_key_length(n, k, q, _leak(n, q))
        rates.append(r.secret_bits / n)
    asym = (1.0 - BB84Protocol.binary_entropy(q)) - _leak(1, q)  # per-bit accounting
    assert rates[0] < rates[1] < rates[2] <= asym + 1e-12
    # μ shrinks like 1/sqrt(k), so convergence is slow but strictly monotone
    # (with the TLGR constant, n = 5e5 / k = 5e4 still sits ~0.11 below asymptotic)
    assert asym - rates[2] < 0.15


def test_serfling_mu_matches_tlgr_constant():
    # TLGR Eq. (2): μ = sqrt((n+k)/(nk) · (k+1)/k · ln(4/ε_sec)); hand-computed for
    # n = k = 10^4, ε_sec = 1e-10:  2e-4 · 1.0001 · ln(4e10) = 2.0002e-4 · 24.41214
    #                                = 4.88291e-3  ->  sqrt = 0.069878
    mu = serfling_mu(10_000, 10_000, 1e-10)
    assert abs(mu - 0.069878) < 5e-6
    # the direct-Serfling variant (factor 1/2, ln(1/ε)) would give ~0.048 -- not this
    assert mu > 0.06
