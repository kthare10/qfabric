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

"""Cascade error reconciliation for QKD sifted keys.

After sifting, Alice's and Bob's keys differ on ~QBER of positions. Cascade is the
standard interactive reconciliation protocol that corrects Bob's key to match
Alice's by exchanging block *parities* over the (authenticated) classical channel:

  * Several passes; pass p uses a fresh shared permutation (pass 0 = identity) and
    a block size that *alternates* k1, 2·k1, k1, 2·k1, … A block whose parity
    differs holds an odd number of errors → BINARY (binary search) finds and flips
    exactly one, in log(k) parities. (Unbounded doubling — the textbook schedule —
    grows blocks toward the whole key, so a residual error *pair* stays co-located
    and is never separated; recurring small blocks fix that: measured 200/200
    perfect reconciliation over QBER 0.005–0.15 vs ~184/200 for doubling.)
  * The "cascade": correcting a bit flips the parity of every block *in earlier
    passes* that contains it — blocks that were even become odd and get re-corrected,
    propagating until stable. This is what drives the residual error rate to ~0.

Every parity bit revealed is public, so ``bits_leaked`` must be subtracted in
privacy amplification. The algorithm is transport-independent: it drives against a
*parity oracle* (a batched callable returning Alice's parity for index sets), so the
same code reconciles in-process (tests) and distributed (RPC to Alice over the link).
Alice's parity for a fixed block never changes (her key is frozen), so those values
are cached — only the dynamic BINARY sub-blocks need fresh queries.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from functools import reduce
from operator import xor

import numpy as np


@dataclass
class CascadeResult:
    """Outcome of a reconciliation run."""
    corrected_key: list[int]
    corrections: int          # bit flips applied to Bob's key
    bits_leaked: int          # parity bits revealed (subtract in privacy amp)
    passes: int
    block_size: int           # pass-0 block size k1
    residual_errors: int | None = None   # set only when Alice's key is known (tests)


def initial_block_size(qber: float, n: int) -> int:
    """Pass-0 block size k1 ≈ 0.73/QBER (Brassard–Salvail), clamped to [1, n]."""
    if qber <= 0.0:
        return max(1, n)
    return max(1, min(n, int(round(0.73 / qber))))


def _parity(key: list[int], block: list[int]) -> int:
    return reduce(xor, (key[i] for i in block), 0)


def reconcile(bob_key: list[int], parity_oracle, qber: float, *,
              passes: int = 4, seed: int = 0) -> CascadeResult:
    """Correct ``bob_key`` toward Alice's using Cascade; return the result.

    Args:
        bob_key: Bob's sifted key bits (corrected in a copy, not in place).
        parity_oracle: callable(list[list[int]]) -> list[int] giving Alice's parity
            over each index list (batched; one call = len(blocks) leaked bits).
        qber: estimated error rate, sets the initial block size.
        passes: number of Cascade passes (4 is standard).
        seed: shared RNG seed for the per-pass permutations (both sides agree).
    """
    n = len(bob_key)
    key = list(bob_key)
    if n == 0:
        return CascadeResult(key, 0, 0, passes, initial_block_size(qber, n))

    leaked = 0

    def alice_parities(blocks: list[list[int]]) -> list[int]:
        nonlocal leaked
        leaked += len(blocks)
        return parity_oracle(blocks)

    k1 = initial_block_size(qber, n)
    rng = np.random.default_rng(seed)

    pass_blocks: list[list[list[int]]] = []   # [pass][block] = index list
    pos_block: list[list[int]] = []           # [pass][pos]   = block id at that pos
    alice_pp: list[list[int]] = []            # [pass][block] = cached Alice parity
    corrections = 0

    for p in range(passes):
        # alternate k1, 2·k1 (capped) — recurring small blocks separate residual
        # error pairs that unbounded doubling would leave co-located.
        bs = min(max(1, n // 2), k1 * (2 ** (p % 2)))
        perm = list(range(n)) if p == 0 else list(rng.permutation(n))
        blocks = [perm[i:i + bs] for i in range(0, n, bs)]
        pb = [0] * n
        for bid, blk in enumerate(blocks):
            for i in blk:
                pb[i] = bid
        pass_blocks.append(blocks)
        pos_block.append(pb)
        alice_pp.append(alice_parities(blocks))     # one batched round trip per pass

        # blocks whose parity disagrees hold an odd number of errors
        work: deque[tuple[int, int]] = deque()
        for bid, blk in enumerate(blocks):
            if alice_pp[p][bid] != _parity(key, blk):
                work.append((p, bid))

        while work:
            q, bid = work.popleft()
            blk = pass_blocks[q][bid]
            # Alice parity is cached & constant; recompute Bob's locally (free)
            if alice_pp[q][bid] == _parity(key, blk):
                continue                             # already even (fixed by a prior flip)
            i = _binary_correct(key, blk, alice_parities)
            corrections += 1
            # cascade: earlier+current passes' blocks containing i may now be odd
            for qq in range(p + 1):
                b2 = pos_block[qq][i]
                if alice_pp[qq][b2] != _parity(key, pass_blocks[qq][b2]):
                    work.append((qq, b2))

    return CascadeResult(
        corrected_key=key,
        corrections=corrections,
        bits_leaked=leaked,
        passes=passes,
        block_size=k1,
    )


def _binary_correct(key: list[int], block: list[int], alice_parities) -> int:
    """Binary-search a block with odd parity for one error; flip it; return its index."""
    b = list(block)
    while len(b) > 1:
        half = b[: len(b) // 2]
        if alice_parities([half])[0] != _parity(key, half):
            b = half
        else:
            b = b[len(b) // 2:]
    i = b[0]
    key[i] ^= 1
    return i


def leak_efficiency(bits_leaked: int, n: int, qber: float) -> float | None:
    """Cascade efficiency f = leaked / (n·H(QBER)); ~1.0 is the Shannon limit."""
    if n == 0 or qber <= 0.0 or qber >= 1.0:
        return None
    h = -qber * math.log2(qber) - (1 - qber) * math.log2(1 - qber)
    denom = n * h
    return bits_leaked / denom if denom > 0 else None
