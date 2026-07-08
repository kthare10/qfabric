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

"""Privacy amplification — extracting the final secret key.

After reconciliation Alice and Bob share an identical key, but an eavesdropper may
hold partial information about it (from channel leakage plus the parities revealed
during Cascade). Privacy amplification compresses the key with a **2-universal hash**
so that whatever the eavesdropper knew about the input tells them essentially nothing
about the shorter output.

We use a random **Toeplitz matrix** T over GF(2): the final key is ``T · key mod 2``,
an m×n matrix defined by only m+n−1 random bits — cheap to share (the hash is public;
its randomness is what matters, not secrecy). Both parties apply the *same* matrix
(same public seed) to their identical reconciled keys, so they get the identical
secret. The output length m is what the security accounting says is safe to keep
(``qne_sequence/reconcile_link.secure_key_bits``).
"""

from __future__ import annotations

import numpy as np


def toeplitz_amplify(key_bits, out_len: int, seed: int) -> list[int]:
    """Compress ``key_bits`` to ``out_len`` bits via a seeded Toeplitz hash (mod 2).

    Args:
        key_bits: the reconciled key as a list of 0/1 ints.
        out_len: desired output length (clamped to [0, len(key_bits)]).
        seed: public seed selecting the Toeplitz matrix — both parties pass the same.

    Returns:
        The extracted secret key as a list of 0/1 ints (length ``out_len``).
    """
    n = len(key_bits)
    m = max(0, min(int(out_len), n))
    if m == 0 or n == 0:
        return []
    rng = np.random.default_rng(seed)
    # A Toeplitz matrix is constant along diagonals, so m+n-1 random bits define it:
    #   T[i, j] = diag[i - j + (n - 1)]
    diag = rng.integers(0, 2, size=m + n - 1, dtype=np.int64)
    idx = (np.arange(m)[:, None] - np.arange(n)[None, :]) + (n - 1)
    T = diag[idx]                                   # m x n over GF(2)
    key = np.asarray(key_bits, dtype=np.int64) & 1
    return ((T @ key) & 1).astype(int).tolist()
