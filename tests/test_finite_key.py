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
    assert asym - rates[2] < 0.1


def test_tighter_eps_costs_key():
    n, k, q = 20_000, 2_000, 0.02
    loose = finite_key_length(n, k, q, _leak(n, q), eps_sec=1e-6, eps_cor=1e-9)
    tight = finite_key_length(n, k, q, _leak(n, q), eps_sec=1e-12, eps_cor=1e-20)
    assert tight.secret_bits < loose.secret_bits


def test_degenerate_inputs():
    assert finite_key_length(0, 0, 0.0, 0.0).secret_bits == 0
    assert serfling_mu(0, 10, 1e-10) == 0.5
    assert serfling_mu(10, 0, 1e-10) == 0.5


def test_measured_leak_beats_planning_estimate():
    # Cascade at low QBER leaks close to n*h(q); the planning estimate charges
    # f_ec=1.16 times that, so a measured leak below it must give MORE key.
    n, k, q = 30_000, 3_000, 0.02
    measured = 1.05 * n * BB84Protocol.binary_entropy(q)
    r_meas = finite_key_length(n, k, q, measured)
    r_plan = finite_key_length(n, k, q, planned_leak(n, q))
    assert r_meas.secret_bits > r_plan.secret_bits
