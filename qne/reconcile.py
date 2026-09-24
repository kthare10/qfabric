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

"""Cascade reconciliation + privacy amplification over a message channel.

After sifting, Bob's key differs from Alice's on the error positions. Bob drives
the Cascade protocol (qne/cascade.py) to correct his key toward Alice's, using her
as a parity oracle over the wire: he sends index sets, she returns her parities.
Only public parities cross the link; each side keeps its own key. When Bob is done
he sends RECONCILE_DONE carrying a short **verification tag** -- a seeded Toeplitz
(2-universal) hash of his corrected key, ``t = ceil(log2(2/ε_cor))`` bits -- plus the
public privacy-amplification seed and output length. Alice compares the tag with her
own and answers RECONCILE_VERIFY; only if the tags match do both sides amplify, so
a residual Cascade error (even-weight, invisible to parities) can never produce two
different "identical" keys silently. The tag is public, so its ``t`` bits are charged
against the key: in asymptotic mode they are subtracted from the PA output, in
finite-key mode the ``log2(2/ε_cor)`` term of the length formula already pays for it.
On mismatch both sides raise ``KeyVerificationError`` and output no key.

The functions drive an *RPC surface* — ``send(kind, body)``, ``call(kind, body,
expected)``, ``recv_any()`` — so one implementation serves every transport:
``qne_sequence.remote_qm.RpcChannel`` (the distributed SeQUeNCe runtime, which
re-exports these names via ``qne_sequence.reconcile_link``) and ``ChannelRpc``
below (the hand-coded raw-socket path's ClassicalChannel).
"""

from __future__ import annotations

from functools import reduce
from operator import xor

import math
import os

from qne.bb84 import BB84Protocol
from qne.cascade import reconcile
from qne.finite_key import finite_key_length
from qne.privacy import toeplitz_amplify

DEFAULT_EPS_COR = 1e-15


class KeyVerificationError(RuntimeError):
    """Alice's and Bob's reconciled keys differ (residual Cascade error)."""

    def __init__(self, corrections: int, bits_leaked: int, side: str):
        super().__init__(f"{side}: verification tag mismatch after Cascade "
                         f"({corrections} corrections, {bits_leaked} bits leaked)")
        self.corrections = corrections
        self.bits_leaked = bits_leaked


def verification_bits(eps_cor: float = DEFAULT_EPS_COR) -> int:
    """Tag length t = ceil(log2(2/ε_cor)) so P[collision on differing keys] ≤ ε_cor/2."""
    return max(1, math.ceil(math.log2(2.0 / eps_cor)))


def verification_tag(key_bits, seed: int, eps_cor: float = DEFAULT_EPS_COR) -> list[int]:
    """Public 2-universal hash of the reconciled key (Toeplitz, public seed)."""
    return toeplitz_amplify(list(key_bits), verification_bits(eps_cor), seed)


def bits_to_int(bits) -> int | None:
    """Comparison token for a key (leading zeros are NOT preserved; None if empty)."""
    return int("".join(str(b) for b in bits), 2) if bits else None


def secure_key_bits(key_bits: int, qber: float, bits_leaked: int, reconciled: bool) -> int:
    """Extractable secret bits after Cascade + privacy amplification.

    PA removes Eve's information ≈ key_bits·H(Q); Cascade already disclosed
    ``bits_leaked`` for error correction, so the secret length is
    key_bits·(1−H(Q)) − bits_leaked. (Not final_key_bits − bits_leaked: the
    Shor–Preskill 1−2H(Q) already charges an asymptotic H(Q) for EC, so
    subtracting the real EC cost too would double-count it.) Zero until reconciled.
    """
    if not reconciled:
        return 0
    h = BB84Protocol.binary_entropy(qber)
    return max(0, int(key_bits * (1.0 - h)) - bits_leaked)


def serve_parities(rpc, key_bits):
    """Alice side: answer parity queries over ``key_bits`` until Bob signals done,
    then apply the same privacy-amplification hash Bob announced.

    Returns (final_key_bits, corrections, bits_leaked) — the extracted secret key.
    """
    while True:
        kind, body = rpc.recv_any()
        if kind == "PARITY_REQ":
            parities = [reduce(xor, (key_bits[i] for i in blk), 0)
                        for blk in body["blocks"]]
            rpc.send("PARITY_RESP", {"parities": parities})
        elif kind == "RECONCILE_DONE":
            tag = verification_tag(key_bits, body["verify_seed"], body["eps_cor"])
            ok = tag == list(body["verify_tag"])
            rpc.send("RECONCILE_VERIFY", {"ok": ok})
            if not ok:
                raise KeyVerificationError(body["corrections"], body["bits_leaked"],
                                           "alice")
            final = toeplitz_amplify(key_bits, body["out_len"], body["pa_seed"])
            return final, body["corrections"], body["bits_leaked"]
        else:
            raise ValueError(f"unexpected frame during reconciliation: {kind}")


def drive_cascade(rpc, key_bits, qber, seed, passes=4, finite=None, *,
                  qber_pa=None, max_out_len=None):
    """Bob side: reconcile ``key_bits`` toward Alice's via Cascade, then privacy-
    amplify to the secure length. Announces the (public) hash seed + output length
    so Alice extracts the identical secret.

    ``qber`` sizes Cascade's blocks (the *bit* error rate). ``qber_pa`` is the error
    rate privacy amplification must erase -- the *phase* error e_x for efficient
    (biased-basis) BB84; default ``qber`` (unbiased: e_z = e_x = Q). Using the bit
    error for PA when e_x > e_z overstates the key (2026-09-21 review).

    ``max_out_len`` caps the PA output -- the decoy-state GLLP budget in decoy mode,
    so the extracted key can never exceed what the single-photon bounds allow.

    ``finite`` switches the PA output length from the asymptotic accounting to the
    finite-key bound (qne/finite_key.py): pass {"n_sample": <QBER sample size>}
    plus optional "eps_sec"/"eps_cor" overrides.

    Returns (final_key_bits, corrections, bits_leaked) — the extracted secret key.
    """
    def parity_oracle(blocks):
        resp = rpc.call("PARITY_REQ",
                        {"blocks": [[int(i) for i in b] for b in blocks]},
                        expected="PARITY_RESP")
        return resp["parities"]

    # floor QBER so a sample that missed all errors doesn't collapse block sizing.
    # Cascade's block permutations are public (announced as index sets); ``seed``
    # only makes the permutation reproducible for tests and is never sent.
    cascade_seed = seed if seed is not None else int.from_bytes(os.urandom(4), "big")
    res = reconcile(list(key_bits), parity_oracle, max(qber, 1.0 / (2 * len(key_bits))),
                    passes=passes, seed=cascade_seed)
    eps_cor = (finite or {}).get("eps_cor", DEFAULT_EPS_COR)
    t = verification_bits(eps_cor)
    q_pa = qber if qber_pa is None else max(qber_pa, 0.0)
    if finite is not None:
        eps = {k: finite[k] for k in ("eps_sec", "eps_cor") if k in finite}
        # finite_key_length already charges log2(2/eps_cor) for this very tag
        out_len = finite_key_length(len(res.corrected_key), finite["n_sample"],
                                    q_pa, res.bits_leaked, **eps).secret_bits
    else:
        # asymptotic accounting: the public t-bit tag is extra leakage
        out_len = max(0, secure_key_bits(len(res.corrected_key), q_pa,
                                         res.bits_leaked, True) - t)
    if max_out_len is not None:
        out_len = max(0, min(out_len, int(max_out_len)))
    # The PA / verification seeds are PUBLIC (announced on the wire), so they must
    # never be derived from the run seed: with ``pa_seed = seed + const`` an
    # eavesdropper recovers the run seed from the transcript and, on a seeded run,
    # regenerates every basis and bit (2026-09-21 review). Always draw them fresh.
    pa_seed = int.from_bytes(os.urandom(4), "big")
    verify_seed = int.from_bytes(os.urandom(4), "big")
    tag = verification_tag(res.corrected_key, verify_seed, eps_cor)
    resp = rpc.call("RECONCILE_DONE",
                    {"corrections": res.corrections, "bits_leaked": res.bits_leaked,
                     "out_len": out_len, "pa_seed": pa_seed,
                     "verify_seed": verify_seed, "verify_tag": tag, "eps_cor": eps_cor},
                    expected="RECONCILE_VERIFY")
    if not resp.get("ok"):
        raise KeyVerificationError(res.corrections, res.bits_leaked, "bob")
    final = toeplitz_amplify(res.corrected_key, out_len, pa_seed)
    return final, res.corrections, res.bits_leaked


class ChannelRpc:
    """Adapt a ``qne.channel.ClassicalChannel`` to the RPC surface above.

    Frames ride the existing length-prefixed JSON protocol as
    ``{"type": <kind>, "body": {...}}``, so the raw-socket Alice/Bob reconcile
    over the same TCP connection they used for sifting.
    """

    def __init__(self, channel):
        self.channel = channel

    def send(self, kind: str, body: dict) -> None:
        self.channel.send_message({"type": kind, "body": body})

    def recv_any(self) -> tuple[str, dict]:
        msg = self.channel.recv_message()
        return msg.get("type"), msg.get("body", {})

    def call(self, kind: str, body: dict, expected: str) -> dict:
        self.send(kind, body)
        got, resp = self.recv_any()
        if got != expected:
            raise ValueError(f"expected {expected!r}, got {got!r}")
        return resp
