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

"""Finite-key security bound for BB84/BBM92.

The asymptotic Shor–Preskill fraction (``BB84Protocol.secure_key_fraction``) assumes
infinitely many signals: the sampled QBER is treated as the true error rate and the
security failure probability is ignored. For a real run of n key bits and k sampled
bits, both assumptions fail — a finite sample can *underestimate* the error rate, and
the extractor/verification steps each carry a failure probability that must be paid
for in key length.

This module implements the standard finite-key recipe (the Tomamichel–Lim–Gisin–
Renner form, Nat. Commun. 3, 634 (2012)):

  1. **Parameter-estimation penalty.** The QBER on the k disclosed bits is corrected
     upward by the Serfling (sampling-without-replacement) fluctuation

         μ = sqrt( (n + k)(k + 1) / (2·n·k²) · ln(1/ε_PE) )

     so that Q_key ≤ Q_obs + μ except with probability ε_PE.
  2. **Extractable length.**

         ℓ = n·(1 − h(Q_obs + μ)) − leak_EC − log2(2/ε_cor) − 2·log2(1/(2·ε_PA))

     ``leak_EC`` is the *measured* Cascade leakage when available (the emulator
     counts every parity bit), else the planning estimate f_EC·n·h(Q). The log
     terms pay for error-verification (ε_cor) and privacy amplification (ε_PA).

The total security parameter is ε_sec ≥ ε_PE + ε_PA (+ smoothing, absorbed into
ε_PA's factor-2 here); we split the caller's ε_sec budget evenly. The point is a
*defensible finite-size penalty* with auditable constants, not squeezing the
tightest known bound.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from qne.bb84 import BB84Protocol


@dataclass
class FiniteKeyResult:
    """Finite-key accounting for one run (all lengths in bits)."""
    secret_bits: int          # extractable ℓ (floored, clamped ≥ 0)
    asymptotic_bits: int      # same accounting minus the finite-size terms
    qber_observed: float
    qber_upper: float         # Q_obs + μ (capped at 0.5)
    mu: float                 # Serfling fluctuation
    n_key: int
    n_sample: int
    leak_ec: float
    eps_sec: float
    eps_cor: float


def serfling_mu(n_key: int, n_sample: int, eps_pe: float) -> float:
    """Serfling fluctuation μ: Q_key ≤ Q_obs + μ except with probability ε_PE."""
    if n_key <= 0 or n_sample <= 0:
        return 0.5
    return math.sqrt((n_key + n_sample) * (n_sample + 1)
                     * math.log(1.0 / eps_pe)
                     / (2.0 * n_key * n_sample ** 2))


def planned_leak(n_key: int, qber: float, f_ec: float = 1.16) -> float:
    """EC leakage estimate f_EC·n·h(Q) — for planning before Cascade has run."""
    return f_ec * n_key * BB84Protocol.binary_entropy(qber)


def finite_key_length(n_key: int, n_sample: int, qber: float, leak_ec: float, *,
                      eps_sec: float = 1e-9, eps_cor: float = 1e-15
                      ) -> FiniteKeyResult:
    """Extractable secret length ℓ for a finite run.

    Args:
        n_key: sifted key bits kept (sample excluded) — the PA input length.
        n_sample: bits disclosed for QBER estimation.
        qber: observed QBER on the sample.
        leak_ec: error-correction leakage in bits (measured Cascade leak, or
            ``planned_leak`` when reconciliation hasn't run yet).
        eps_sec: total security failure budget (split evenly PE/PA).
        eps_cor: correctness failure budget (error-verification hash).
    """
    h = BB84Protocol.binary_entropy
    eps_pe = eps_pa = eps_sec / 2.0
    mu = serfling_mu(n_key, n_sample, eps_pe)
    q_up = min(0.5, qber + mu)

    asymptotic = max(0, int(n_key * (1.0 - h(qber)) - leak_ec))
    if n_key <= 0 or n_sample <= 0:
        return FiniteKeyResult(0, asymptotic, qber, q_up, mu, n_key, n_sample,
                               leak_ec, eps_sec, eps_cor)

    ell = (n_key * (1.0 - h(q_up))
           - leak_ec
           - math.log2(2.0 / eps_cor)
           - 2.0 * math.log2(1.0 / (2.0 * eps_pa)))
    return FiniteKeyResult(
        secret_bits=max(0, math.floor(ell)),
        asymptotic_bits=asymptotic,
        qber_observed=qber,
        qber_upper=q_up,
        mu=mu,
        n_key=n_key,
        n_sample=n_sample,
        leak_ec=leak_ec,
        eps_sec=eps_sec,
        eps_cor=eps_cor,
    )
