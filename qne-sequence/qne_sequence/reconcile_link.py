"""Cascade reconciliation over a Link — shared by the E91 and BB84 paths.

After sifting, Bob's key differs from Alice's on the error positions. Bob drives the
Cascade protocol (qne/cascade.py) to correct his key toward Alice's, using her as a
parity oracle over the wire: he sends index sets, she returns her parities. Only
public parities cross the link; each side keeps its own key. When Bob is done he
sends RECONCILE_DONE and both hold the identical key.

Both endpoints share the same three frame kinds (PARITY_REQ / PARITY_RESP /
RECONCILE_DONE) on an RpcChannel, so E91 and BB84 reconcile through this one module.
"""

from __future__ import annotations

from functools import reduce
from operator import xor

from qne.bb84 import BB84Protocol
from qne.cascade import reconcile
from qne.privacy import toeplitz_amplify


def bits_to_int(bits) -> int | None:
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
            final = toeplitz_amplify(key_bits, body["out_len"], body["pa_seed"])
            return final, body["corrections"], body["bits_leaked"]
        else:
            raise ValueError(f"unexpected frame during reconciliation: {kind}")


def drive_cascade(rpc, key_bits, qber, seed, passes=4):
    """Bob side: reconcile ``key_bits`` toward Alice's via Cascade, then privacy-
    amplify to the secure length. Announces the (public) hash seed + output length
    so Alice extracts the identical secret.

    Returns (final_key_bits, corrections, bits_leaked) — the extracted secret key.
    """
    def parity_oracle(blocks):
        resp = rpc.call("PARITY_REQ",
                        {"blocks": [[int(i) for i in b] for b in blocks]},
                        expected="PARITY_RESP")
        return resp["parities"]

    # floor QBER so a sample that missed all errors doesn't collapse block sizing
    res = reconcile(list(key_bits), parity_oracle, max(qber, 1.0 / (2 * len(key_bits))),
                    passes=passes, seed=seed)
    out_len = secure_key_bits(len(res.corrected_key), qber, res.bits_leaked, True)
    pa_seed = seed + 111
    final = toeplitz_amplify(res.corrected_key, out_len, pa_seed)
    rpc.send("RECONCILE_DONE", {"corrections": res.corrections,
                                "bits_leaked": res.bits_leaked,
                                "out_len": out_len, "pa_seed": pa_seed})
    return final, res.corrections, res.bits_leaked
